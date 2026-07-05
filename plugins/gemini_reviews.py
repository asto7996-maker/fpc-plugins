"""
Плагин: автоответы на отзывы Starvell через Google Gemini.
Требует настроенные прокси и API-ключ Gemini в настройках бота.
"""

from __future__ import annotations

from ai_service import AIService
from starvell_sdk import StarvellPlugin, ReviewReplyContext, on_pre_review

NAME = "Gemini — отзывы"
UUID = "b7e2f4a1-9c3d-4e8f-a2b6-1d5c8e7f0a4b"
VERSION = "1.0.0"
DESCRIPTION = "Умные ответы на отзывы покупателей через Google Gemini"
CREDITS = "Starvell Cardinal"
SETTINGS_PAGE = True

DEFAULT_TEMPLATES = {
    "5": "Большое спасибо за отличный отзыв! ⭐ Рады, что всё понравилось!",
    "4": "Спасибо за хорошую оценку! 😊 Будем рады видеть вас снова!",
    "3": "Спасибо за отзыв! Если что-то можно улучшить — напишите нам в чат.",
    "2": "Жаль, что не всё идеально. Мы уже работаем над улучшением сервиса.",
    "1": "Нам очень жаль за негативный опыт. Напишите в чат — разберёмся лично.",
}


class Plugin(StarvellPlugin):
    def __init__(self, core, config=None):
        super().__init__(core, config)
        self._ai: AIService | None = None

    async def on_startup(self) -> None:
        self._ai = AIService(self.settings)
        self.log("Gemini Reviews v%s ready", self.VERSION)

    @on_pre_review
    async def reply_with_gemini(self, ctx: ReviewReplyContext) -> None:
        if not await self.get_cfg("enabled", True):
            return

        settings = self.settings
        if not settings.is_gemini_proxy_configured():
            self.log("Прокси Gemini не настроен — пропуск отзыва %s", ctx.order_id, level="warning")
            return
        if not settings.is_gemini_configured():
            self.log("Gemini API ключ не настроен — пропуск отзыва %s", ctx.order_id, level="warning")
            return

        order = ctx.order
        review = order.get("review") or {}
        stars_raw = review.get("stars") or review.get("rating")
        try:
            stars = int(stars_raw) if stars_raw is not None else 0
        except (TypeError, ValueError):
            stars = 0
        comment = str(review.get("comment") or review.get("text") or review.get("content") or "").strip()

        if await self.get_cfg("require_buyer_review", False) and stars < 1:
            ctx.skipped = True
            return

        if stars < 1:
            stars = 5

        use_gemini = await self.get_cfg("use_gemini", True)
        if not self._ai:
            self._ai = AIService(settings)
        else:
            self._ai.settings = settings

        if use_gemini:
            text = await self._ai.generate_review_reply(order, stars=stars, comment=comment)
        else:
            templates = await self.get_cfg("templates", DEFAULT_TEMPLATES)
            if not isinstance(templates, dict):
                templates = DEFAULT_TEMPLATES
            text = str(templates.get(str(stars)) or templates.get("5") or settings.review_template)

        if not text:
            return

        api = ctx.api()
        if api:
            text = api.apply_watermark(text, settings.watermark_on, settings.watermark_text)
        ctx.reply_text = text

    def get_settings_schema(self) -> list[dict]:
        return [
            {
                "key": "enabled",
                "label": "Включить автоответы на отзывы",
                "type": "bool",
                "default": True,
            },
            {
                "key": "use_gemini",
                "label": "Генерировать ответ через Gemini",
                "type": "bool",
                "default": True,
            },
            {
                "key": "require_buyer_review",
                "label": "Только если покупатель оставил отзыв",
                "type": "bool",
                "default": False,
            },
        ]
