"""
Модуль фильтрации контента.
Определяет, подходит ли пост для репоста на основании правил из конфигурации:
ключевые слова, длина текста, тип медиа, минимальные просмотры, обнаружение рекламы.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """Результат проверки фильтрами."""
    passed: bool
    reason: str = ""
    filter_name: str = ""


class ContentFilters:
    """Система фильтрации постов."""

    def __init__(self, config: Dict[str, Any]):
        self._config = config
        self._compiled_regex_wl: List[re.Pattern] = []
        self._compiled_regex_bl: List[re.Pattern] = []
        self._compile_regex_patterns()

    def update_config(self, config: Dict[str, Any]) -> None:
        """Обновляет конфигурацию фильтров."""
        self._config = config
        self._compile_regex_patterns()

    def _compile_regex_patterns(self) -> None:
        """Компилирует регулярные выражения из конфигурации."""
        self._compiled_regex_wl = []
        self._compiled_regex_bl = []

        for pattern in self._config.get("regex_whitelist", []):
            try:
                self._compiled_regex_wl.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning("Невалидный regex в whitelist: %s — %s", pattern, e)

        for pattern in self._config.get("regex_blacklist", []):
            try:
                self._compiled_regex_bl.append(re.compile(pattern, re.IGNORECASE))
            except re.error as e:
                logger.warning("Невалидный regex в blacklist: %s — %s", pattern, e)

    def check_all(
        self,
        text: str,
        media_type: str = "",
        views: int = 0,
        is_forward: bool = False,
        has_media: bool = False,
    ) -> FilterResult:
        """
        Выполняет все проверки по порядку.
        Возвращает FilterResult(passed=True) если пост прошёл все фильтры.
        """
        checks = [
            self._check_text_length(text),
            self._check_min_views(views),
            self._check_media_type(media_type),
            self._check_required_media(media_type, has_media),
            self._check_keyword_blacklist(text),
            self._check_keyword_whitelist(text),
            self._check_regex_blacklist(text),
            self._check_regex_whitelist(text),
            self._check_ads(text),
            self._check_forward(is_forward),
        ]

        for result in checks:
            if not result.passed:
                logger.debug(
                    "Пост не прошёл фильтр '%s': %s",
                    result.filter_name,
                    result.reason,
                )
                return result

        return FilterResult(passed=True)

    def _check_text_length(self, text: str) -> FilterResult:
        """Проверяет длину текста."""
        min_len = self._config.get("min_text_length", 0)
        max_len = self._config.get("max_text_length", 0)
        text_len = len(text.strip()) if text else 0

        if min_len > 0 and text_len < min_len:
            return FilterResult(
                passed=False,
                reason=f"Текст слишком короткий: {text_len} < {min_len}",
                filter_name="text_length",
            )
        if max_len > 0 and text_len > max_len:
            return FilterResult(
                passed=False,
                reason=f"Текст слишком длинный: {text_len} > {max_len}",
                filter_name="text_length",
            )
        return FilterResult(passed=True, filter_name="text_length")

    def _check_min_views(self, views: int) -> FilterResult:
        """Проверяет минимальное количество просмотров."""
        min_views = self._config.get("min_views", 0)
        if min_views > 0 and views < min_views:
            return FilterResult(
                passed=False,
                reason=f"Недостаточно просмотров: {views} < {min_views}",
                filter_name="min_views",
            )
        return FilterResult(passed=True, filter_name="min_views")

    def _check_media_type(self, media_type: str) -> FilterResult:
        """Проверяет, не заблокирован ли тип медиа."""
        blocked = self._config.get("blocked_media_types", [])
        if blocked and media_type and media_type.lower() in [b.lower() for b in blocked]:
            return FilterResult(
                passed=False,
                reason=f"Тип медиа '{media_type}' в блок-листе",
                filter_name="media_type_blocked",
            )
        return FilterResult(passed=True, filter_name="media_type_blocked")

    def _check_required_media(self, media_type: str, has_media: bool) -> FilterResult:
        """Проверяет, требуется ли определённый тип медиа."""
        required = self._config.get("required_media_types", [])
        if not required:
            return FilterResult(passed=True, filter_name="required_media")

        if not has_media or not media_type:
            return FilterResult(
                passed=False,
                reason="Пост без медиа, а фильтр требует медиа",
                filter_name="required_media",
            )

        if media_type.lower() not in [r.lower() for r in required]:
            return FilterResult(
                passed=False,
                reason=f"Тип '{media_type}' не в списке требуемых: {required}",
                filter_name="required_media",
            )
        return FilterResult(passed=True, filter_name="required_media")

    def _check_keyword_blacklist(self, text: str) -> FilterResult:
        """Проверяет чёрный список ключевых слов."""
        blacklist = self._config.get("keyword_blacklist", [])
        if not blacklist or not text:
            return FilterResult(passed=True, filter_name="keyword_blacklist")

        text_lower = text.lower()
        for keyword in blacklist:
            if keyword.lower() in text_lower:
                return FilterResult(
                    passed=False,
                    reason=f"Найдено запрещённое слово: '{keyword}'",
                    filter_name="keyword_blacklist",
                )
        return FilterResult(passed=True, filter_name="keyword_blacklist")

    def _check_keyword_whitelist(self, text: str) -> FilterResult:
        """
        Проверяет белый список ключевых слов.
        Если whitelist задан, пост должен содержать хотя бы одно слово из списка.
        """
        whitelist = self._config.get("keyword_whitelist", [])
        if not whitelist:
            return FilterResult(passed=True, filter_name="keyword_whitelist")

        if not text:
            return FilterResult(
                passed=False,
                reason="Пост без текста, а whitelist требует ключевые слова",
                filter_name="keyword_whitelist",
            )

        text_lower = text.lower()
        for keyword in whitelist:
            if keyword.lower() in text_lower:
                return FilterResult(passed=True, filter_name="keyword_whitelist")

        return FilterResult(
            passed=False,
            reason="Не найдено ни одного слова из whitelist",
            filter_name="keyword_whitelist",
        )

    def _check_regex_blacklist(self, text: str) -> FilterResult:
        """Проверяет чёрный список regex."""
        if not self._compiled_regex_bl or not text:
            return FilterResult(passed=True, filter_name="regex_blacklist")

        for pattern in self._compiled_regex_bl:
            if pattern.search(text):
                return FilterResult(
                    passed=False,
                    reason=f"Совпадение с regex-блокировкой: {pattern.pattern}",
                    filter_name="regex_blacklist",
                )
        return FilterResult(passed=True, filter_name="regex_blacklist")

    def _check_regex_whitelist(self, text: str) -> FilterResult:
        """
        Проверяет белый список regex.
        Если regex_whitelist задан, текст должен совпадать хотя бы с одним паттерном.
        """
        if not self._compiled_regex_wl:
            return FilterResult(passed=True, filter_name="regex_whitelist")

        if not text:
            return FilterResult(
                passed=False,
                reason="Пост без текста, а regex_whitelist задан",
                filter_name="regex_whitelist",
            )

        for pattern in self._compiled_regex_wl:
            if pattern.search(text):
                return FilterResult(passed=True, filter_name="regex_whitelist")

        return FilterResult(
            passed=False,
            reason="Не найдено совпадений с regex_whitelist",
            filter_name="regex_whitelist",
        )

    def _check_ads(self, text: str) -> FilterResult:
        """Проверяет текст на наличие рекламных маркеров."""
        if not self._config.get("skip_ads", True):
            return FilterResult(passed=True, filter_name="ad_check")
        if not text:
            return FilterResult(passed=True, filter_name="ad_check")

        ad_keywords = self._config.get("ad_keywords", [])
        if not ad_keywords:
            return FilterResult(passed=True, filter_name="ad_check")

        text_lower = text.lower()
        for keyword in ad_keywords:
            if keyword.lower() in text_lower:
                return FilterResult(
                    passed=False,
                    reason=f"Обнаружена реклама (маркер: '{keyword}')",
                    filter_name="ad_check",
                )
        return FilterResult(passed=True, filter_name="ad_check")

    def _check_forward(self, is_forward: bool) -> FilterResult:
        """Проверяет, является ли пост пересылкой (forward)."""
        skip_forwards = self._config.get("skip_forwards", False)
        if skip_forwards and is_forward:
            return FilterResult(
                passed=False,
                reason="Пост является пересылкой, пропускаем",
                filter_name="forward_check",
            )
        return FilterResult(passed=True, filter_name="forward_check")


class TextProcessor:
    """Обработка текста постов перед публикацией."""

    @staticmethod
    def remove_links(text: str) -> str:
        """Удаляет все ссылки из текста."""
        url_pattern = re.compile(
            r'https?://[^\s<>"{}|\\^`\[\]]+', re.IGNORECASE
        )
        text = url_pattern.sub("", text)
        tg_link = re.compile(r'@[\w]+', re.IGNORECASE)
        text = tg_link.sub("", text)
        return text.strip()

    @staticmethod
    def remove_hashtags(text: str) -> str:
        """Удаляет хештеги из текста."""
        return re.sub(r'#\S+', '', text).strip()

    @staticmethod
    def clean_whitespace(text: str) -> str:
        """Нормализует пробельные символы."""
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        return text.strip()

    @staticmethod
    def apply_caption_template(
        template: str,
        original_caption: str = "",
        donor_name: str = "",
        link: str = "",
    ) -> str:
        """
        Подставляет значения в шаблон подписи.
        Доступные переменные: {caption}, {donor}, {link}, {newline}
        """
        if not template:
            return original_caption

        result = template
        result = result.replace("{caption}", original_caption or "")
        result = result.replace("{donor}", donor_name or "")
        result = result.replace("{link}", link or "")
        result = result.replace("{newline}", "\n")
        result = result.replace("\\n", "\n")
        return result.strip()

    @classmethod
    def process_caption(
        cls,
        original_caption: str,
        config: Dict[str, Any],
        donor_name: str = "",
    ) -> str:
        """
        Полная обработка подписи поста.
        Применяет настройки из конфигурации: удаление ссылок, шаблоны и т.д.
        """
        caption = original_caption or ""

        if config.get("remove_original_caption", False):
            caption = ""

        if config.get("remove_original_links", False) and caption:
            caption = cls.remove_links(caption)

        caption = cls.clean_whitespace(caption)

        template = config.get("caption_template", "")
        link = config.get("append_link", "")

        if template:
            caption = cls.apply_caption_template(
                template, caption, donor_name, link
            )
        elif link:
            if caption:
                caption = f"{caption}\n\n{link}"
            else:
                caption = link

        if config.get("add_signature", False):
            signature = config.get("signature_text", "")
            if signature:
                caption = f"{caption}\n\n{signature}" if caption else signature

        if len(caption) > 4096:
            caption = caption[:4090] + "..."

        return caption
