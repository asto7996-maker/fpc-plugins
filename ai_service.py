"""
Интеграция Google Gemini — ИИ-консультант в чатах Starvell.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import Settings
from validators import GEMINI_MODELS, gemini_auth_headers, gemini_generate_url, parse_gemini_api_key

logger = logging.getLogger("starvell.ai")


class AIService:
    """Генерация ответов через Gemini."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": 45.0}
        proxy = self.settings.gemini_proxy_url()
        if proxy:
            kwargs["proxy"] = proxy
        return kwargs

    def _api_key(self) -> str:
        return parse_gemini_api_key(self.settings.gemini_api_key)

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
        if not self._api_key():
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

    async def translate_text(self, text: str, target_lang: str = "en") -> str:
        if not text.strip():
            return text
        if not self._api_key():
            return text
        lang = "английский" if target_lang == "en" else target_lang
        prompt = (
            f"Переведи текст на {lang}. Сохрани форматирование, эмодзи и переносы строк.\n"
            f"Верни только перевод без пояснений.\n\n{text}"
        )
        result = await self._gemini(prompt)
        return result or text

    async def generate_review_text(self, order: dict[str, Any]) -> str:
        review = order.get("review") or {}
        stars = int(review.get("stars") or review.get("rating") or 5)
        comment = str(review.get("comment") or review.get("text") or "").strip()
        return await self.generate_review_reply(order, stars=stars, comment=comment)

    async def generate_review_reply(
        self,
        order: dict[str, Any],
        *,
        stars: int = 5,
        comment: str = "",
    ) -> str:
        offer = order.get("offerDetails") or {}
        desc = (offer.get("descriptions") or {}).get("rus") or {}
        product = desc.get("briefDescription") or desc.get("description") or "товар"
        buyer = (order.get("user") or {}).get("username") or "покупатель"
        stars = max(1, min(5, int(stars or 5)))

        if comment:
            prompt = (
                f"Покупатель {buyer} оставил отзыв {stars}/5 на товар «{product}».\n"
                f"Текст отзыва: «{comment[:500]}»\n\n"
                "Напиши короткий ответ продавца на отзыв (2-3 предложения, тёплый тон, 1-3 эмодзи). "
                "Учти оценку и текст отзыва. Не обещай возврат средств."
            )
        else:
            prompt = (
                f"Покупатель {buyer} завершил заказ «{product}» (оценка {stars}/5).\n"
                "Напиши короткое благодарственное сообщение (2-3 предложения, тёплый тон, 1-3 эмодзи)."
            )

        text = await self._gemini(prompt)
        if text:
            return text

        fallback = self.settings.review_replies.get(str(stars)) or self.settings.review_template
        return fallback

    async def _gemini(self, user_prompt: str) -> str | None:
        api_key = self._api_key()
        if not api_key:
            return None

        models = [self.settings.gemini_model] + [m for m in GEMINI_MODELS if m != self.settings.gemini_model]
        payload_base = {
            "systemInstruction": {"parts": [{"text": self.settings.ai_system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0.85, "maxOutputTokens": 600},
        }

        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            headers = gemini_auth_headers(api_key)
            for model in models:
                url = gemini_generate_url(model)
                try:
                    resp = await client.post(url, json=payload_base, headers=headers)
                    if resp.status_code != 200:
                        logger.debug("Gemini %s HTTP %s: %s", model, resp.status_code, resp.text[:200])
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
