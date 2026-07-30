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
        f"Цель: дружелюбно подсказать сервис поддержки {bot}.\n"
        "Правила:\n"
        "1) Пиши кратко и естественно, 1–2 предложения, на языке исходного сообщения.\n"
        "2) Не используй HTTP/HTTPS ссылки и домены (.com, .ru и т.п.).\n"
        "3) Не используй рекламные клише вроде «лучший VPN», «переходи по ссылке».\n"
        f"4) Упоминай сервис только как Telegram-юзернейм (например {bot} / поиск в Telegram).\n"
        "5) Не выдавай себя за официальную поддержку чата; тон — участник комьюнити.\n"
        "6) Верни только текст черновика, без кавычек и пояснений."
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

        # Soft sanitize: strip bare urls if model slipped
        cleaned = _strip_urls(text)
        return cleaned[:500]


def _strip_urls(text: str) -> str:
    import re

    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\b\w+\.(com|ru|net|org|io)\b", "", text, flags=re.I)
    return " ".join(text.split())
