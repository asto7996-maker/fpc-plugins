"""
Интеграция Google Gemini — ИИ-консультант в чатах Starvell.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import Settings
from validators import GEMINI_MODELS

logger = logging.getLogger("starvell.ai")


class AIService:
    """Генерация ответов через Gemini."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _build_prompt(self, buyer_message: str, chat_history: list[dict[str, Any]], product_hint: str = "") -> str:
        history_lines = []
        for msg in chat_history[-15:]:
            text = str(msg.get("content") or msg.get("text") or "").strip()
            if text:
                history_lines.append(f"- {text}")
        history_text = "\n".join(history_lines) if history_lines else "История пуста."
        parts = [
            "Покупатель написал в чат магазина Starvell.",
            f"Сообщение: {buyer_message}",
            f"История:\n{history_text}",
        ]
        if product_hint:
            parts.append(f"Товар: {product_hint}")
        parts.append(
            "Ответь кратко (до 500 символов), вежливо, на русском. "
            "Не обещай возврат средств."
        )
        return "\n\n".join(parts)

    async def generate_reply(
        self,
        buyer_message: str,
        chat_history: list[dict[str, Any]] | None = None,
        product_hint: str = "",
    ) -> str | None:
        if not self.settings.gemini_api_key.strip():
            return None
        blocked = self.check_blacklist(buyer_message)
        if blocked:
            logger.warning("AI blacklist triggered: %s", blocked)
            return None
        prompt = self._build_prompt(buyer_message, chat_history or [], product_hint)
        return await self._gemini(prompt)

    def check_blacklist(self, text: str) -> str | None:
        """Возвращает найденное слово или None."""
        text_l = text.lower()
        for word in self.settings.ai_word_blacklist:
            w = word.strip().lower()
            if w and w in text_l:
                return w
        return None

    async def generate_review_text(self, order: dict[str, Any]) -> str:
        offer = order.get("offerDetails") or {}
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        product = desc.get("briefDescription") or desc.get("description") or "товар"
        prompt = (
            f"Напиши короткое благодарственное сообщение покупателю "
            f"за покупку «{product}». 2-3 предложения, тёплый тон, 2-3 эмодзи."
        )
        text = await self._gemini(prompt)
        return text or self.settings.review_template

    async def _gemini(self, user_prompt: str) -> str | None:
        api_key = self.settings.gemini_api_key.strip()
        if not api_key:
            return None

        models = [self.settings.gemini_model] + [m for m in GEMINI_MODELS if m != self.settings.gemini_model]
        payload_base = {
            "systemInstruction": {"parts": [{"text": self.settings.ai_system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.85, "maxOutputTokens": 600},
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            for model in models:
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={api_key}"
                )
                try:
                    resp = await client.post(url, json=payload_base)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if not candidates:
                        continue
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                    if text:
                        return text[:800]
                except Exception as exc:
                    logger.warning("Gemini %s: %s", model, exc)
        return None
