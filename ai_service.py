"""
Интеграция с OpenAI и Google Gemini для авто-ответов в чатах Starvell.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import Settings

logger = logging.getLogger("starvell.ai")


class AIService:
    """Генерация ответов через OpenAI или Gemini."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _build_prompt(self, buyer_message: str, chat_history: list[dict[str, Any]], product_hint: str = "") -> str:
        history_lines = []
        for msg in chat_history[-15:]:
            author = msg.get("author") or msg.get("authorId") or "?"
            text = str(msg.get("content") or msg.get("text") or "").strip()
            if text:
                history_lines.append(f"- {author}: {text}")
        history_text = "\n".join(history_lines) if history_lines else "История пуста."
        parts = [
            "Покупатель написал сообщение в чат магазина Starvell.",
            f"Сообщение: {buyer_message}",
            f"История чата:\n{history_text}",
        ]
        if product_hint:
            parts.append(f"Контекст товара: {product_hint}")
        parts.append(
            "Ответь кратко (до 500 символов), вежливо, по делу, на русском. "
            "Не обещай возврат средств. Напомни о правилах магазина только если спрашивают о возврате."
        )
        return "\n\n".join(parts)

    async def generate_reply(
        self,
        buyer_message: str,
        chat_history: list[dict[str, Any]] | None = None,
        product_hint: str = "",
    ) -> str | None:
        """Генерирует ответ ИИ на сообщение покупателя."""
        prompt = self._build_prompt(buyer_message, chat_history or [], product_hint)
        provider = (self.settings.ai_provider or "gemini").lower()

        if provider == "openai" and self.settings.openai_api_key:
            return await self._openai(prompt)
        if self.settings.gemini_api_key:
            return await self._gemini(prompt)
        if self.settings.openai_api_key:
            return await self._openai(prompt)
        logger.warning("AI ключи не настроены")
        return None

    async def generate_review_text(self, order: dict[str, Any]) -> str:
        """Генерирует благодарственный текст после закрытия сделки."""
        offer = order.get("offerDetails") or {}
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        product = desc.get("briefDescription") or desc.get("description") or "товар"
        buyer = (order.get("user") or {}).get("username") or "покупатель"
        prompt = (
            f"Напиши короткое благодарственное сообщение покупателю {buyer} "
            f"за покупку «{product}». 2-3 предложения, тёплый тон, 2-3 эмодзи, без имени."
        )
        provider = (self.settings.ai_provider or "gemini").lower()
        if provider == "openai" and self.settings.openai_api_key:
            text = await self._openai(prompt)
        elif self.settings.gemini_api_key:
            text = await self._gemini(prompt)
        else:
            text = None
        return text or self.settings.review_template

    async def _gemini(self, user_prompt: str) -> str | None:
        api_key = self.settings.gemini_api_key.strip()
        if not api_key:
            return None
        model = self.settings.gemini_model or "gemini-2.0-flash"
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        payload = {
            "systemInstruction": {"parts": [{"text": self.settings.ai_system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.85, "maxOutputTokens": 600},
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error("Gemini HTTP %s: %s", resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    return None
                parts = candidates[0].get("content", {}).get("parts", [])
                text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                return text[:800] if text else None
        except Exception as exc:
            logger.exception("Gemini error: %s", exc)
            return None

    async def _openai(self, user_prompt: str) -> str | None:
        api_key = self.settings.openai_api_key.strip()
        if not api_key:
            return None
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.settings.openai_model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": self.settings.ai_system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.85,
            "max_tokens": 600,
        }
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code != 200:
                    logger.error("OpenAI HTTP %s: %s", resp.status_code, resp.text[:200])
                    return None
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return None
                text = (choices[0].get("message") or {}).get("content", "").strip()
                return text[:800] if text else None
        except Exception as exc:
            logger.exception("OpenAI error: %s", exc)
            return None
