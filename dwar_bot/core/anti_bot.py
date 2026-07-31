"""
Антибот-защита и эмуляция живого игрока для Dwar.

- HumanBehavior: кривые Безье, idle-паузы, human_type с опечатками
- CaptchaHandler: детекция капчи, скриншоты, Telegram, ожидание manual override
- AntiBot: фасад для main_loop (совместимость с оркестратором)
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, List, Optional, Sequence, Tuple

from playwright.async_api import (
    ElementHandle,
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from dwar_bot.config import CAPTCHAS_DIR, BotConfig, config

logger = logging.getLogger(__name__)

Point = Tuple[float, float]

RE_CAPTCHA = re.compile(
    r"(?:captcha|капч|подтвердите.*(?:человек|не\s+робот)|"
    r"я\s+не\s+робот|are\s+you\s+a\s+human|recaptcha|hcaptcha|"
    r"cloudflare|challenge-platform|cf-challenge)",
    re.IGNORECASE,
)
RE_ANTIBOT = re.compile(
    r"(?:подозрительн|слишком\s+быстр|антибот|проверка\s+активност|"
    r"заблокир|unusual\s+activity|bot\s+detect)",
    re.IGNORECASE,
)

CAPTCHA_SELECTORS: Tuple[str, ...] = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='captcha']",
    "iframe[title*='captcha' i]",
    "iframe[title*='challenge' i]",
    ".g-recaptcha",
    ".h-captcha",
    "#captcha",
    ".captcha",
    "[data-captcha]",
    "#cf-challenge-running",
    ".cf-browser-verification",
    ".challenge-form",
    "img[src*='captcha']",
    "input[name*='captcha' i]",
)

NEARBY_KEYS = {
    "a": "sqw",
    "b": "vgh",
    "c": "xdf",
    "d": "sfe",
    "e": "wr",
    "f": "dg",
    "g": "fh",
    "h": "gj",
    "i": "uo",
    "j": "hk",
    "k": "jl",
    "l": "k",
    "m": "n",
    "n": "bm",
    "o": "ip",
    "p": "o",
    "q": "wa",
    "r": "et",
    "s": "ad",
    "t": "ry",
    "u": "yi",
    "v": "cb",
    "w": "qe",
    "x": "zc",
    "y": "tu",
    "z": "x",
    "а": "вп",
    "б": "ью",
    "в": "аы",
    "г": "нш",
    "д": "лж",
    "е": "кну",
    "ё": "е",
    "ж": "дэ",
    "з": "щх",
    "и": "шм",
    "й": "цф",
    "к": "уе",
    "л": "од",
    "м": "иь",
    "н": "гт",
    "о": "лр",
    "п": "ра",
    "р": "по",
    "с": "чы",
    "т": "нь",
    "у": "ке",
    "ф": "ыя",
    "х": "зъ",
    "ц": "йу",
    "ч": "сб",
    "ш": "щи",
    "щ": "зш",
    "ъ": "хэ",
    "ы": "вф",
    "ь": "тб",
    "э": "ж",
    "ю": "б",
    "я": "чф",
}


# ---------------------------------------------------------------------------
# HumanBehavior
# ---------------------------------------------------------------------------


class HumanBehavior:
    """Эмуляция движений мыши, пауз и набора текста живого игрока."""

    def __init__(self, bot_config: Optional[BotConfig] = None) -> None:
        self._config = bot_config or config
        self._cursor: Point = (
            self._config.browser.viewport_width / 2.0,
            self._config.browser.viewport_height / 2.0,
        )

    @property
    def cursor(self) -> Point:
        return self._cursor

    def set_cursor(self, x: float, y: float) -> None:
        self._cursor = (float(x), float(y))

    # ---- Bezier mouse -------------------------------------------------

    @staticmethod
    def _cubic_bezier(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
        u = 1.0 - t
        tt = t * t
        uu = u * u
        uuu = uu * u
        ttt = tt * t
        x = uuu * p0[0] + 3 * uu * t * p1[0] + 3 * u * tt * p2[0] + ttt * p3[0]
        y = uuu * p0[1] + 3 * uu * t * p1[1] + 3 * u * tt * p2[1] + ttt * p3[1]
        return (x, y)

    def _ease_in_out(self, t: float) -> float:
        """Ускорение в начале, замедление в конце (smoothstep-подобно)."""
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    def _build_bezier_path(
        self,
        start: Point,
        end: Point,
        *,
        steps: Optional[int] = None,
    ) -> List[Point]:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        distance = math.hypot(dx, dy)
        if steps is None:
            steps = max(12, min(48, int(distance / 12) + random.randint(8, 16)))

        # Контрольные точки — дуга со случайным изгибом
        mid_x = (start[0] + end[0]) / 2.0
        mid_y = (start[1] + end[1]) / 2.0
        # Перпендикулярный оффсет
        if distance < 1:
            nx, ny = 0.0, 0.0
        else:
            nx, ny = -dy / distance, dx / distance
        bend = random.uniform(0.15, 0.45) * distance
        bend *= random.choice([-1.0, 1.0])
        # Две контрольные точки вдоль пути с шумом
        c1 = (
            start[0] + dx * random.uniform(0.2, 0.4) + nx * bend * random.uniform(0.4, 0.9),
            start[1] + dy * random.uniform(0.2, 0.4) + ny * bend * random.uniform(0.4, 0.9),
        )
        c2 = (
            start[0] + dx * random.uniform(0.6, 0.85) + nx * bend * random.uniform(0.2, 0.6),
            start[1] + dy * random.uniform(0.6, 0.85) + ny * bend * random.uniform(0.2, 0.6),
        )

        points: List[Point] = []
        for i in range(steps + 1):
            linear_t = i / steps
            eased = self._ease_in_out(linear_t)
            x, y = self._cubic_bezier(start, c1, c2, end, eased)
            # Микро-колебания (дрожание руки), меньше у цели
            jitter_amp = 1.8 * (1.0 - eased)
            x += random.uniform(-jitter_amp, jitter_amp)
            y += random.uniform(-jitter_amp, jitter_amp)
            points.append((x, y))
        points[-1] = end
        return points

    async def move_along_bezier(
        self,
        page: Page,
        end: Point,
        *,
        start: Optional[Point] = None,
    ) -> Point:
        """Ведёт курсор по кривой Безье до точки ``end``."""
        origin = start or self._cursor
        path = self._build_bezier_path(origin, end)
        n = len(path)
        for idx, (x, y) in enumerate(path):
            await page.mouse.move(x, y)
            # Быстрее в середине, медленнее у краёв
            progress = idx / max(1, n - 1)
            edge = min(progress, 1.0 - progress)
            delay = 0.004 + (0.018 * (1.0 - edge * 2.0)) + random.uniform(0.0, 0.006)
            await asyncio.sleep(max(0.002, delay))
        self._cursor = end
        return end

    async def bezier_mouse_move(
        self,
        page: Page,
        target_selector: str,
        *,
        frame: Optional[Frame] = None,
        element: Optional[ElementHandle] = None,
        timeout_ms: Optional[int] = None,
    ) -> Point:
        """
        Рассчитывает кривую Безье от текущей позиции курсора до элемента
        и перемещает мышь по дуге с ускорением/замедлением.
        """
        owner: Any = frame or page
        timeout = timeout_ms or self._config.browser.default_timeout_ms

        if element is None:
            try:
                await owner.wait_for_selector(
                    target_selector, timeout=timeout, state="visible"
                )
                element = await owner.query_selector(target_selector)
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    f"bezier_mouse_move: элемент не найден: {target_selector}"
                ) from exc

        if element is None:
            raise RuntimeError(f"bezier_mouse_move: элемент не найден: {target_selector}")

        box = await element.bounding_box()
        if box is None:
            raise RuntimeError(
                f"bezier_mouse_move: нет bounding_box у {target_selector}"
            )

        dx, dy = self._config.delays.click_offset()
        margin_x = max(1.0, box["width"] * 0.12)
        margin_y = max(1.0, box["height"] * 0.12)
        target_x = box["x"] + box["width"] / 2 + dx
        target_y = box["y"] + box["height"] / 2 + dy
        target_x = min(
            max(target_x, box["x"] + margin_x),
            box["x"] + box["width"] - margin_x,
        )
        target_y = min(
            max(target_y, box["y"] + margin_y),
            box["y"] + box["height"] - margin_y,
        )

        end = await self.move_along_bezier(page, (target_x, target_y))
        logger.debug(
            "bezier_mouse_move -> (%.1f, %.1f) selector=%s",
            end[0],
            end[1],
            target_selector[:80],
        )
        return end

    # ---- Idle ---------------------------------------------------------

    async def random_idle(self, page: Page, chance: float = 0.15) -> float:
        """
        С вероятностью ``chance`` имитирует задумчивость (3–12 сек):
        случайные движения мыши, scroll, выделение текста.
        """
        chance = max(0.0, min(1.0, chance))
        if random.random() >= chance:
            return 0.0

        duration = random.uniform(3.0, 12.0)
        logger.info("HumanBehavior random_idle: %.1f сек", duration)
        deadline = time.monotonic() + duration

        while time.monotonic() < deadline:
            action = random.choice(["wander", "scroll", "select", "pause"])
            try:
                if action == "wander":
                    await self._idle_wander(page)
                elif action == "scroll":
                    await self._idle_scroll(page)
                elif action == "select":
                    await self._idle_select_text(page)
                else:
                    await asyncio.sleep(random.uniform(0.4, 1.2))
            except PlaywrightError as exc:
                logger.debug("random_idle action failed: %s", exc)
                await asyncio.sleep(random.uniform(0.3, 0.8))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, random.uniform(0.2, 0.7)))

        return duration

    async def _idle_wander(self, page: Page) -> None:
        viewport = page.viewport_size or {
            "width": self._config.browser.viewport_width,
            "height": self._config.browser.viewport_height,
        }
        end = (
            random.uniform(viewport["width"] * 0.15, viewport["width"] * 0.85),
            random.uniform(viewport["height"] * 0.15, viewport["height"] * 0.85),
        )
        await self.move_along_bezier(page, end)

    async def _idle_scroll(self, page: Page) -> None:
        delta = random.randint(-280, 280)
        if delta == 0:
            delta = random.choice([-120, 120])
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.2, 0.6))

    async def _idle_select_text(self, page: Page) -> None:
        """Пытается выделить случайный видимый текстовый узел тройным кликом."""
        try:
            handle = await page.evaluate_handle(
                """() => {
                    const nodes = Array.from(document.querySelectorAll(
                        'p, span, a, td, div, label, li'
                    )).filter(el => {
                        const t = (el.innerText || '').trim();
                        if (t.length < 4 || t.length > 80) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 20 && r.height > 8
                            && r.top >= 0 && r.left >= 0
                            && r.bottom < window.innerHeight
                            && r.right < window.innerWidth;
                    });
                    if (!nodes.length) return null;
                    return nodes[Math.floor(Math.random() * nodes.length)];
                }"""
            )
            element = handle.as_element()
            if element is None:
                return
            box = await element.bounding_box()
            if box is None:
                return
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await self.move_along_bezier(page, (x, y))
            await page.mouse.click(x, y, click_count=3)
            await asyncio.sleep(random.uniform(0.3, 0.9))
            # Снимаем выделение кликом в сторону
            await page.mouse.click(
                max(5, x - random.uniform(40, 90)),
                max(5, y + random.uniform(20, 50)),
            )
        except PlaywrightError as exc:
            logger.debug("idle select text: %s", exc)

    # ---- Typing -------------------------------------------------------

    async def human_type(
        self,
        page: Page,
        selector: str,
        text: str,
        *,
        frame: Optional[Frame] = None,
        clear_first: bool = True,
        typo_chance: float = 0.05,
    ) -> None:
        """
        Ввод текста с разной задержкой между клавишами и ~5% опечаток.
        """
        owner: Any = frame or page
        timeout = self._config.browser.default_timeout_ms

        try:
            await owner.wait_for_selector(selector, timeout=timeout, state="visible")
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"human_type: поле не найдено: {selector}") from exc

        # Фокус через bezier + click
        try:
            await self.bezier_mouse_move(page, selector, frame=frame)
            await asyncio.sleep(random.uniform(0.08, 0.25))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.04, 0.1))
            await page.mouse.up()
        except Exception as exc:
            logger.debug("human_type focus via bezier failed: %s — fallback click", exc)
            await owner.click(selector, timeout=timeout)

        if clear_first:
            try:
                await page.keyboard.down("Control")
                await page.keyboard.press("KeyA")
                await page.keyboard.up("Control")
                await asyncio.sleep(random.uniform(0.05, 0.12))
                await page.keyboard.press("Backspace")
            except PlaywrightError:
                try:
                    await owner.fill(selector, "")
                except PlaywrightError as exc:
                    logger.debug("human_type clear failed: %s", exc)

        await asyncio.sleep(random.uniform(0.1, 0.35))

        for char in text:
            if (
                typo_chance > 0
                and char.isalnum()
                and random.random() < typo_chance
            ):
                wrong = self._typo_char(char)
                await page.keyboard.type(wrong, delay=0)
                await asyncio.sleep(random.uniform(0.12, 0.35))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.08, 0.2))

            await page.keyboard.type(char, delay=0)
            await asyncio.sleep(random.uniform(0.05, 0.25))

            # Редкая более длинная пауза «подумал»
            if random.random() < 0.04:
                await asyncio.sleep(random.uniform(0.35, 0.9))

        logger.debug("human_type: введено %s символов в %s", len(text), selector)

    @staticmethod
    def _typo_char(char: str) -> str:
        lower = char.lower()
        neighbors = NEARBY_KEYS.get(lower, "")
        if neighbors:
            pick = random.choice(neighbors)
            return pick.upper() if char.isupper() else pick
        pool = string.ascii_lowercase if char.isascii() else "абвгдежзиклмнопрстуфхцчшщыэюя"
        pick = random.choice(pool)
        return pick.upper() if char.isupper() else pick


# ---------------------------------------------------------------------------
# CaptchaHandler
# ---------------------------------------------------------------------------


class CaptchaHandler:
    """
    Детекция капчи и ожидание ручного решения (manual override).

    Ставит глобальный pause-флаг, который main_loop может опрашивать
    через ``is_paused`` / ``wait_until_resumed``.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        captchas_dir: Optional[Path] = None,
        screenshot_fn: Optional[Callable[..., Awaitable[Optional[Path]]]] = None,
    ) -> None:
        self._config = bot_config or config
        self._captchas_dir = captchas_dir or CAPTCHAS_DIR
        self._screenshot_fn = screenshot_fn
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # initially NOT paused (set = running)
        self._paused = False
        self._last_captcha_at: float = 0.0
        self._last_screenshot: Optional[Path] = None

    @property
    def is_paused(self) -> bool:
        return self._paused

    def resume(self) -> None:
        """Снимает флаг паузы (manual override)."""
        self._paused = False
        self._pause_event.set()
        logger.info("CaptchaHandler: manual override — пауза снята")

    def request_pause(self) -> None:
        self._paused = True
        self._pause_event.clear()

    async def wait_until_resumed(self, *, poll_sec: float = 2.0) -> None:
        """Блокируется, пока капча не исчезнет или не вызовут resume()."""
        while self._paused:
            try:
                await asyncio.wait_for(self._pause_event.wait(), timeout=poll_sec)
            except asyncio.TimeoutError:
                continue

    async def detect_captcha(self, page: Page) -> bool:
        """Сканирует DOM и iframe на признаки капчи / антибот-предупреждений."""
        try:
            # Основной документ
            if await self._page_has_captcha(page):
                return True

            # Все фреймы игры
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    if await self._frame_has_captcha(frame):
                        logger.critical(
                            "Капча во фрейме: name=%s url=%s",
                            frame.name,
                            (frame.url or "")[:120],
                        )
                        return True
                except PlaywrightError as exc:
                    logger.debug("frame captcha scan: %s", exc)
        except PlaywrightError as exc:
            logger.debug("detect_captcha failed: %s", exc)
        return False

    async def _page_has_captcha(self, page: Page) -> bool:
        try:
            content = await page.content()
        except PlaywrightError:
            content = ""

        if RE_CAPTCHA.search(content) or RE_ANTIBOT.search(content):
            logger.critical("Капча/антибот по HTML-маркерам на page")
            return True

        for selector in CAPTCHA_SELECTORS:
            try:
                handle = await page.query_selector(selector)
                if handle is None:
                    continue
                visible = True
                try:
                    visible = await handle.is_visible()
                except PlaywrightError:
                    visible = True
                if visible:
                    logger.critical("Капча по селектору page: %s", selector)
                    return True
            except PlaywrightError:
                continue
        return False

    async def _frame_has_captcha(self, frame: Frame) -> bool:
        url = (frame.url or "").lower()
        if any(
            token in url
            for token in ("captcha", "recaptcha", "hcaptcha", "challenge", "cloudflare")
        ):
            return True

        try:
            content = await frame.content()
        except PlaywrightError:
            try:
                content = str(
                    await frame.evaluate("() => document.body ? document.body.innerText : ''")
                    or ""
                )
            except PlaywrightError:
                return False

        if RE_CAPTCHA.search(content) or RE_ANTIBOT.search(content):
            return True

        for selector in CAPTCHA_SELECTORS:
            try:
                handle = await frame.query_selector(selector)
                if handle is not None:
                    return True
            except PlaywrightError:
                continue
        return False

    async def handle_captcha_alert(
        self,
        page: Page,
        log: Optional[logging.Logger] = None,
        *,
        auto_wait: bool = True,
        max_wait_sec: float = 600.0,
    ) -> bool:
        """
        При обнаружении капчи:
          1) ставит главный цикл на паузу;
          2) скриншот в captchas/;
          3) Telegram: «ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Появилась капча!»;
          4) ждёт manual_override или исчезновения капчи.
        """
        log = log or logger
        if not await self.detect_captcha(page):
            return False

        self.request_pause()
        self._last_captcha_at = time.time()
        log.critical("ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Появилась капча!")

        shot = await self._save_captcha_screenshot(page)
        self._last_screenshot = shot
        if shot:
            log.critical("Скриншот капчи: %s", shot)

        try:
            from dwar_bot.logger import notify_telegram

            await notify_telegram(
                "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Появилась капча!\n"
                f"screenshot={shot or 'n/a'}\n"
                "Пройдите проверку вручную и дождитесь снятия паузы.",
                critical=True,
            )
        except Exception as exc:
            log.debug("Telegram notify failed: %s", exc)

        # Звуковой сигнал в терминал (BEL)
        try:
            import sys

            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

        if not auto_wait:
            return True

        deadline = time.monotonic() + max_wait_sec
        log.critical(
            "Ожидание ручного решения капчи (до %.0f сек). "
            "Вызовите captcha_handler.resume() после прохождения.",
            max_wait_sec,
        )

        while time.monotonic() < deadline and self._paused:
            await asyncio.sleep(random.uniform(2.0, 4.0))
            # Автоснятие, если капча исчезла
            try:
                still = await self.detect_captcha(page)
            except Exception:
                still = True
            if not still:
                log.info("Капча исчезла — снимаем паузу автоматически")
                self.resume()
                break
            if not self._paused:
                break

        if self._paused:
            log.error(
                "Таймаут ожидания капчи (%.0f сек) — пауза остаётся активной",
                max_wait_sec,
            )
        return True

    async def _save_captcha_screenshot(self, page: Page) -> Optional[Path]:
        self._captchas_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = self._captchas_dir / f"captcha_{stamp}.png"

        if self._screenshot_fn is not None:
            try:
                custom = await self._screenshot_fn(prefix="captcha")
                if custom is not None:
                    return custom
            except Exception as exc:
                logger.debug("custom screenshot_fn failed: %s", exc)

        try:
            await page.screenshot(path=str(path), full_page=True)
            return path
        except PlaywrightError as exc:
            logger.error("Не удалось сохранить скриншот капчи: %s", exc)
            return None


# ---------------------------------------------------------------------------
# AntiBot facade (для main.py)
# ---------------------------------------------------------------------------


class AntiBot:
    """
    Фасад: HumanBehavior + CaptchaHandler для оркестратора.

    Сохраняет API ``tick`` / ``handle_challenge`` / ``detect_captcha``.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        browser: Any = None,
    ) -> None:
        self._config = bot_config or config
        self._browser = browser
        self.behavior = HumanBehavior(self._config)
        screenshot_fn = None
        if browser is not None and hasattr(browser, "capture_error_screenshot"):
            screenshot_fn = browser.capture_error_screenshot
        self.captcha = CaptchaHandler(
            self._config, screenshot_fn=screenshot_fn
        )
        self._actions_since_pause = 0

    @property
    def is_paused(self) -> bool:
        return self.captcha.is_paused

    def resume(self) -> None:
        self.captcha.resume()

    async def maybe_idle_pause(self, page: Optional[Page] = None, *, force: bool = False) -> float:
        self._actions_since_pause += 1
        chance = 0.15
        if force:
            chance = 1.0
        elif self._actions_since_pause >= random.randint(8, 20):
            chance = 0.55

        if page is None:
            if random.random() >= chance and not force:
                delay = random.uniform(0.2, 0.8)
                await asyncio.sleep(delay)
                return delay
            pause = random.uniform(3.0, 12.0)
            logger.info("AntiBot idle (no page): %.1f сек", pause)
            await asyncio.sleep(pause)
            self._actions_since_pause = 0
            return pause

        duration = await self.behavior.random_idle(page, chance=chance)
        if duration > 0:
            self._actions_since_pause = 0
        return duration

    async def human_mouse_wander(self, page: Page) -> None:
        await self.behavior._idle_wander(page)  # noqa: SLF001

    async def detect_captcha(self, page: Page) -> bool:
        return await self.captcha.detect_captcha(page)

    async def handle_challenge(self, page: Page) -> bool:
        return await self.captcha.handle_captcha_alert(page, logger)

    async def handle_captcha_alert(
        self, page: Page, log: Optional[logging.Logger] = None
    ) -> bool:
        return await self.captcha.handle_captcha_alert(page, log or logger)

    async def bezier_mouse_move(self, page: Page, target_selector: str, **kwargs: Any) -> Point:
        return await self.behavior.bezier_mouse_move(page, target_selector, **kwargs)

    async def human_type(self, page: Page, selector: str, text: str, **kwargs: Any) -> None:
        await self.behavior.human_type(page, selector, text, **kwargs)

    async def tick(self, page: Page) -> None:
        if self.captcha.is_paused:
            await self.captcha.wait_until_resumed()
            return
        await self.maybe_idle_pause(page)
        if random.random() < 0.35:
            await self.human_mouse_wander(page)
        await self.handle_challenge(page)
