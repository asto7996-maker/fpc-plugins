"""
Async Google Gemini client for draft generation (google-genai SDK).
"""

from __future__ import annotations

import logging
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"


def build_system_instruction(promote_username: str) -> str:
    """
    Operator-facing draft prompt.

    Generates a short recommendation of the support username (no bare URLs).
    Drafts are reviewed by a human unless auto_approve is explicitly enabled.
    """
    bot = promote_username if promote_username.startswith("@") else f"@{promote_username}"
    return (
        "Ты помогаешь оператору поддержки составить короткий черновик ответа "
        "в Telegram-чат.\n"
        f"Цель: по-человечески подсказать сервис {bot}.\n"
        "Жёсткие правила стиля (обязательно):\n"
        "1) Начинай сообщение с маленькой буквы (lowercase start).\n"
        "2) Без эмодзи и смайликов вообще — только чистый текст и @юзернейм.\n"
        "3) Естественная пунктуация чата: запятые/точки ок, без книжной вычурности.\n"
        "4) Без капса, без «!!!», без рекламных штампов "
        "(«лучший VPN», «переходи по ссылке», «акция»).\n"
        "5) 1–2 коротких предложения, язык как у собеседника.\n"
        "6) Не используй HTTP/HTTPS ссылки и домены (.com, .ru и т.п.).\n"
        f"7) Упоминай сервис только как Telegram-юзернейм (например {bot}).\n"
        "8) Не выдавай себя за админа чата — тон обычного участника.\n"
        "9) Верни только текст черновика, без кавычек и пояснений."
    )


class GeminiDraftClient:
    """Thin async wrapper around google-genai Client.aio."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        promote_username: str = "@PaskodVPN_bot",
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key is empty")
        self.api_key = api_key
        self.model = model
        self.promote_username = promote_username
        self._client = genai.Client(api_key=api_key)

    async def generate_draft(
        self,
        *,
        source_text: str,
        chat_title: Optional[str] = None,
        matched_trigger: Optional[str] = None,
    ) -> str:
        system = build_system_instruction(self.promote_username)
        user_prompt = (
            f"Чат: {chat_title or 'unknown'}\n"
            f"Триггер: {matched_trigger or 'n/a'}\n"
            f"Сообщение пользователя:\n{source_text.strip()}\n\n"
            "Составь черновик ответа."
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.9,
                    max_output_tokens=180,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gemini generate_content failed")
            raise RuntimeError(f"Gemini API error: {exc}") from exc

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            # Fallback parse
            try:
                cand = response.candidates[0].content.parts[0].text  # type: ignore[index]
                text = (cand or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
        if not text:
            raise RuntimeError("Gemini returned empty draft")

        # Soft sanitize: strip bare urls if model slipped, then humanize stylistics
        from tg_pool.core.humanize import humanize_text_sync

        cleaned = humanize_text_sync(_strip_urls(text))
        return cleaned[:500]


def _strip_urls(text: str) -> str:
    import re

    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\b\w+\.(com|ru|net|org|io)\b", "", text, flags=re.I)
    return " ".join(text.split())
