"""
Имитация человеческого поведения и реакция на антибот-проверки.

Случайные паузы, микродвижения мыши, детекция капчи/модалок.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Optional, Sequence

from playwright.async_api import Error as PlaywrightError, Page

from dwar_bot.config import BotConfig, config
from dwar_bot.core.browser import BrowserEngine

logger = logging.getLogger(__name__)

RE_CAPTCHA = re.compile(
    r"(?:captcha|капч|подтвердите.*человек|я\s+не\s+робот|recaptcha|hcaptcha)",
    re.IGNORECASE,
)
RE_ANTIBOT = re.compile(
    r"(?:подозрительн|слишком\s+быстр|антибот|проверка|заблокир)",
    re.IGNORECASE,
)


class AntiBot:
    """Human-like паузы и детекция антибот-событий."""

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        browser: Optional[BrowserEngine] = None,
    ) -> None:
        self._config = bot_config or config
        self._browser = browser
        self._last_action_at = time.monotonic()
        self._actions_since_pause = 0

    async def maybe_idle_pause(self, *, force: bool = False) -> float:
        """
        Случайная длинная пауза «игрок отошёл».

        Returns:
            Фактическая длительность паузы в секундах.
        """
        delays = self._config.delays
        self._actions_since_pause += 1

        should = force
        if not should and self._actions_since_pause >= random.randint(8, 20):
            should = True
        if not should and random.random() < 0.08:
            should = True

        if not should:
            short = random.uniform(delays.action_min * 0.3, delays.action_max * 0.5)
            await asyncio.sleep(short)
            return short

        pause = random.uniform(delays.idle_min, delays.idle_max)
        logger.info("AntiBot idle-пауза %.1f сек", pause)
        await asyncio.sleep(pause)
        self._actions_since_pause = 0
        self._last_action_at = time.monotonic()
        return pause

    async def human_mouse_wander(self, page: Page) -> None:
        """Небольшое случайное движение мыши без клика."""
        try:
            viewport = page.viewport_size or {
                "width": self._config.browser.viewport_width,
                "height": self._config.browser.viewport_height,
            }
            x = random.uniform(viewport["width"] * 0.2, viewport["width"] * 0.8)
            y = random.uniform(viewport["height"] * 0.2, viewport["height"] * 0.8)
            await page.mouse.move(x, y, steps=random.randint(4, 12))
            await asyncio.sleep(random.uniform(0.05, 0.2))
        except PlaywrightError as exc:
            logger.debug("mouse wander failed: %s", exc)

    async def detect_captcha(self, page: Page) -> bool:
        """Проверяет страницу на признаки капчи / антибот-модалки."""
        try:
            content = await page.content()
        except PlaywrightError as exc:
            logger.debug("detect_captcha content: %s", exc)
            return False

        if RE_CAPTCHA.search(content) or RE_ANTIBOT.search(content):
            logger.critical("Обнаружена капча / антибот-проверка!")
            return True

        selectors: Sequence[str] = (
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            ".g-recaptcha",
            "#captcha",
            ".captcha",
            "[data-captcha]",
        )
        for selector in selectors:
            try:
                handle = await page.query_selector(selector)
                if handle is not None and await handle.is_visible():
                    logger.critical("Капча по селектору: %s", selector)
                    return True
            except PlaywrightError:
                continue
        return False

    async def handle_challenge(self, page: Page) -> bool:
        """
        Реакция на антибот: длинная пауза, уведомление, без авто-решения капчи.

        Returns:
            True если вызов обнаружен (цикл должен затормозить / остановиться).
        """
        if not await self.detect_captcha(page):
            return False

        # Длинная «человеческая» пауза вместо агрессивных действий
        pause = random.uniform(25.0, 60.0)
        logger.critical(
            "Антибот-вызов: пауза %.0f сек, требуется ручное вмешательство",
            pause,
        )
        try:
            from dwar_bot.logger import notify_telegram

            await notify_telegram(
                "Обнаружена капча/антибот! Бот на паузе. "
                "Пройдите проверку вручную.",
                critical=True,
            )
        except Exception as exc:
            logger.debug("telegram notify: %s", exc)

        if self._browser is not None:
            await self._browser.capture_error_screenshot(prefix="captcha")

        await asyncio.sleep(pause)
        # Повторная проверка
        still = await self.detect_captcha(page)
        if still:
            logger.critical("Капча всё ещё активна после паузы")
        return True

    async def tick(self, page: Page) -> None:
        """Вызов из main_loop: пауза + лёгкий wander + проверка капчи."""
        await self.maybe_idle_pause()
        if random.random() < 0.35:
            await self.human_mouse_wander(page)
        await self.handle_challenge(page)
