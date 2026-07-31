"""Имитация человеческого поведения для обхода античит-систем.

Модуль предоставляет :class:`HumanBehavior` — набор асинхронных помощников:
    * случайные задержки заданных диапазонов;
    * «человеческие» движения мыши по кривой Безье;
    * клики с наведением и микропаузами;
    * набор текста по одному символу со случайной скоростью;
    * случайный скроллинг страницы;
    * периодические длинные перерывы («отход от ПК»).

Все методы устойчивы к ошибкам Playwright: сбой имитации не должен ронять
основную логику, поэтому мышиные действия оборачиваются в try/except.
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional, Tuple

from playwright.async_api import Error as PlaywrightError, Page

from ..config import CONFIG
from ..logger import get_logger

logger = get_logger(__name__)


class HumanBehavior:
    """Помощник для human-like взаимодействия со страницей."""

    def __init__(self, page: Optional[Page] = None) -> None:
        self._page = page
        self._delays = CONFIG.delays
        # Текущее «положение курсора» для расчёта плавных перемещений.
        self._cursor: Tuple[float, float] = (
            random.uniform(0, CONFIG.browser.viewport_width),
            random.uniform(0, CONFIG.browser.viewport_height),
        )

    def bind(self, page: Page) -> None:
        """Привязывает поведение к конкретной странице."""
        self._page = page

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("HumanBehavior не привязан к странице")
        return self._page

    # ------------------------------------------------------------------ #
    #  Задержки                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    async def sleep(min_s: float, max_s: float) -> float:
        """Спит случайное время в диапазоне [min_s, max_s]. Возвращает время."""
        if max_s < min_s:
            min_s, max_s = max_s, min_s
        delay = random.uniform(max(0.0, min_s), max(0.0, max_s))
        await asyncio.sleep(delay)
        return delay

    async def action_pause(self) -> float:
        """Короткая пауза между простыми действиями."""
        return await self.sleep(self._delays.action_min, self._delays.action_max)

    async def read_pause(self) -> float:
        """Пауза «чтения» после навигации/появления контента."""
        return await self.sleep(
            self._delays.page_read_min, self._delays.page_read_max
        )

    async def loop_pause(self) -> float:
        """Пауза между итерациями основного цикла."""
        return await self.sleep(self._delays.loop_min, self._delays.loop_max)

    async def maybe_long_break(self) -> bool:
        """С некоторой вероятностью делает длинный перерыв.

        Возвращает ``True``, если перерыв был сделан.
        """
        if random.random() < self._delays.long_break_chance:
            duration = random.uniform(
                self._delays.long_break_min, self._delays.long_break_max
            )
            logger.info("Длинный перерыв (имитация AFK): %.0f сек", duration)
            await asyncio.sleep(duration)
            return True
        return False

    # ------------------------------------------------------------------ #
    #  Движения мыши                                                      #
    # ------------------------------------------------------------------ #
    async def move_mouse_to(self, x: float, y: float, steps: Optional[int] = None) -> None:
        """Плавно перемещает курсор к (x, y) по кривой Безье."""
        try:
            start_x, start_y = self._cursor
            # Контрольные точки для лёгкого изгиба траектории.
            ctrl_x = (start_x + x) / 2 + random.uniform(-60, 60)
            ctrl_y = (start_y + y) / 2 + random.uniform(-60, 60)

            total_steps = steps or random.randint(18, 34)
            for i in range(1, total_steps + 1):
                t = i / total_steps
                # Квадратичная кривая Безье.
                bx = (1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t ** 2 * x
                by = (1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t ** 2 * y
                await self.page.mouse.move(bx, by)
                await asyncio.sleep(random.uniform(0.005, 0.02))
            self._cursor = (x, y)
        except PlaywrightError as exc:
            logger.debug("move_mouse_to сбой: %s", exc)

    async def _hover_element(self, element) -> bool:
        """Наводит курсор на центр элемента с лёгким случайным сдвигом."""
        try:
            box = await element.bounding_box()
            if not box:
                return False
            target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await self.move_mouse_to(target_x, target_y)
            return True
        except PlaywrightError as exc:
            logger.debug("_hover_element сбой: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    #  Клики / ввод                                                       #
    # ------------------------------------------------------------------ #
    async def click(self, selector: str, timeout_ms: Optional[int] = None) -> bool:
        """Человекоподобный клик по селектору.

        Ждёт элемент, наводит курсор, делает микропаузу и кликает.
        Возвращает ``True`` при успехе.
        """
        try:
            element = await self.page.wait_for_selector(
                selector,
                timeout=timeout_ms or CONFIG.browser.default_timeout_ms,
                state="visible",
            )
        except PlaywrightError:
            logger.debug("click: селектор не найден: %s", selector)
            return False

        if element is None:
            return False

        await self._hover_element(element)
        await self.action_pause()
        try:
            await element.click(delay=random.uniform(40, 130))
            return True
        except PlaywrightError as exc:
            logger.debug("click сбой на %s: %s", selector, exc)
            # Фолбэк: программный клик.
            try:
                await element.click(force=True)
                return True
            except PlaywrightError:
                return False

    async def type_text(self, selector: str, text: str, clear: bool = True) -> bool:
        """Набирает текст по одному символу со случайной скоростью."""
        try:
            element = await self.page.wait_for_selector(
                selector,
                timeout=CONFIG.browser.default_timeout_ms,
                state="visible",
            )
        except PlaywrightError:
            logger.debug("type_text: селектор не найден: %s", selector)
            return False
        if element is None:
            return False

        await self._hover_element(element)
        await self.action_pause()
        try:
            await element.click()
            if clear:
                await element.fill("")
            for char in text:
                await self.page.keyboard.type(char)
                await asyncio.sleep(
                    random.uniform(self._delays.typing_min, self._delays.typing_max)
                )
            return True
        except PlaywrightError as exc:
            logger.debug("type_text сбой на %s: %s", selector, exc)
            return False

    # ------------------------------------------------------------------ #
    #  Скролл                                                             #
    # ------------------------------------------------------------------ #
    async def random_scroll(self, max_delta: int = 500) -> None:
        """Случайный скролл страницы вверх/вниз."""
        try:
            delta = random.randint(-max_delta // 3, max_delta)
            await self.page.mouse.wheel(0, delta)
            await self.sleep(0.2, 0.8)
        except PlaywrightError as exc:
            logger.debug("random_scroll сбой: %s", exc)

    async def idle_wander(self) -> None:
        """Небольшое «бесцельное» движение мыши — фоновая активность."""
        x = random.uniform(0, CONFIG.browser.viewport_width)
        y = random.uniform(0, CONFIG.browser.viewport_height)
        await self.move_mouse_to(x, y, steps=random.randint(10, 20))


__all__ = ["HumanBehavior"]
