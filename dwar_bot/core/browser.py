"""
Асинхронный браузерный движок на Playwright для Dwar.

Возможности:
  - эмуляция реального ПК-браузера (UA, viewport 1920x1080);
  - отключение navigator.webdriver и связанных флагов автоматизации;
  - применение cookie-сессий в BrowserContext;
  - безопасная навигация goto_with_retry с обработкой сетевых таймаутов;
  - human_click по кривой Безье + пауза перед кликом;
  - human_type с опечатками и разным темпом набора;
  - скриншоты при ошибках (если включено в конфиге).
"""

from __future__ import annotations

import asyncio
import logging
import random
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Error as PlaywrightError,
    Frame,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from dwar_bot.auth.cookie_manager import CookieManager, CookieSession
from dwar_bot.config import SCREENSHOTS_DIR, BotConfig, config
from dwar_bot.core.anti_bot import HumanBehavior

logger = logging.getLogger(__name__)

SelectorLike = Union[str, ElementHandle]


class BrowserEngineError(Exception):
    """Общая ошибка браузерного движка."""


class NavigationError(BrowserEngineError):
    """Не удалось выполнить навигацию после всех попыток."""


# Скрипт, внедряемый до загрузки каждой страницы — снимает webdriver-флаги
_STEALTH_INIT_SCRIPT = """
(() => {
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
            configurable: true,
        });
    } catch (e) {}

    try {
        // chrome runtime stub
        window.chrome = window.chrome || { runtime: {} };
    } catch (e) {}

    try {
        const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (originalQuery) {
            window.navigator.permissions.query = (parameters) => (
                parameters && parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
        }
    } catch (e) {}

    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
            configurable: true,
        });
    } catch (e) {}

    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['ru-RU', 'ru', 'en-US', 'en'],
            configurable: true,
        });
    } catch (e) {}

    try {
        // Playwright/Chromium иногда оставляет CDC-свойства
        for (const key of Object.getOwnPropertyNames(window)) {
            if (key.match(/^cdc_|__playwright|__pw_/)) {
                try { delete window[key]; } catch (e) {}
            }
        }
    } catch (e) {}
})();
"""


class BrowserEngine:
    """Обёртка над Playwright async API для игрового бота."""

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        cookie_manager: Optional[CookieManager] = None,
    ) -> None:
        self._config = bot_config or config
        self._browser_cfg = self._config.browser
        self._delays = self._config.delays
        self._cookie_manager = cookie_manager or CookieManager(self._config)

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._started: bool = False
        self._network_log: List[Dict[str, Any]] = []
        self._human = HumanBehavior(self._config)

    @property
    def human(self) -> HumanBehavior:
        """Доступ к HumanBehavior (Bezier / idle / type)."""
        return self._human

    # ------------------------------------------------------------------
    # Свойства
    # ------------------------------------------------------------------

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserEngineError("Браузер не инициализирован: вызовите start()")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise BrowserEngineError("Контекст не создан: вызовите start()")
        return self._context

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise BrowserEngineError("Браузер не запущен: вызовите start()")
        return self._browser

    @property
    def cookie_manager(self) -> CookieManager:
        return self._cookie_manager

    @property
    def is_started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BrowserEngine":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            await self.capture_error_screenshot(prefix="exit_error")
        await self.stop()

    async def start(self, *, apply_cookies: bool = True) -> Page:
        """
        Инициализация Playwright с эмуляцией реального браузера.

        - User-Agent ПК
        - viewport 1920x1080
        - отключение navigator.webdriver
        - опциональное применение cookie-сессии
        """
        if self._started:
            logger.warning("BrowserEngine уже запущен")
            return self.page

        self._config.ensure_directories()
        logger.info(
            "Запуск BrowserEngine (headless=%s, viewport=%sx%s)",
            self._browser_cfg.headless,
            self._browser_cfg.viewport_width,
            self._browser_cfg.viewport_height,
        )

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._browser_cfg.headless,
                slow_mo=self._browser_cfg.slow_mo_ms,
                args=list(self._browser_cfg.chrome_args),
            )
            self._context = await self._browser.new_context(
                viewport={
                    "width": self._browser_cfg.viewport_width,
                    "height": self._browser_cfg.viewport_height,
                },
                user_agent=self._browser_cfg.user_agent,
                locale=self._browser_cfg.locale,
                timezone_id=self._browser_cfg.timezone_id,
                ignore_https_errors=self._browser_cfg.ignore_https_errors,
                java_script_enabled=True,
                accept_downloads=False,
                color_scheme="light",
                has_touch=False,
                is_mobile=False,
                device_scale_factor=1,
            )
            self._context.set_default_timeout(self._browser_cfg.default_timeout_ms)
            self._context.set_default_navigation_timeout(
                self._browser_cfg.navigation_timeout_ms
            )

            await self._context.add_init_script(_STEALTH_INIT_SCRIPT)

            if self._browser_cfg.permissions:
                await self._context.grant_permissions(
                    list(self._browser_cfg.permissions),
                    origin=self._config.server.base_url,
                )

            self._page = await self._context.new_page()
            self._attach_network_listeners(self._page)

            if apply_cookies:
                await self.apply_cookies()

            # Дополнительно снимаем webdriver на уже открытой странице
            await self._patch_webdriver(self._page)

            self._started = True
            logger.info("BrowserEngine успешно инициализирован")
            return self._page
        except Exception as exc:
            logger.error("Ошибка инициализации браузера: %s", exc, exc_info=True)
            await self.stop()
            raise BrowserEngineError(f"Не удалось запустить браузер: {exc}") from exc

    async def stop(self) -> None:
        """Корректное закрытие страницы, контекста и браузера."""
        self._started = False

        # Сохраняем cookie перед закрытием
        if self._context is not None:
            try:
                await self._cookie_manager.sync_from_playwright(self._context)
            except Exception as exc:
                logger.warning(
                    "Не удалось сохранить cookie при остановке: %s",
                    exc,
                    exc_info=True,
                )

        for label, closer in (
            ("page", self._close_page),
            ("context", self._close_context),
            ("browser", self._close_browser),
            ("playwright", self._close_playwright),
        ):
            try:
                await closer()
            except Exception as exc:
                logger.debug("Ошибка при закрытии %s: %s", label, exc)

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("BrowserEngine остановлен")

    async def _close_page(self) -> None:
        if self._page is not None and not self._page.is_closed():
            await self._page.close()

    async def _close_context(self) -> None:
        if self._context is not None:
            await self._context.close()

    async def _close_browser(self) -> None:
        if self._browser is not None and self._browser.is_connected():
            await self._browser.close()

    async def _close_playwright(self) -> None:
        if self._playwright is not None:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Cookie
    # ------------------------------------------------------------------

    async def apply_cookies(
        self, session: Optional[CookieSession] = None
    ) -> CookieSession:
        """Применяет cookie активной (или переданной) сессии в browser_context."""
        context = self.context

        if session is not None:
            host = self._config.server.base_url
            default_domain = ".dwar.ru"
            cookies = session.get_playwright_cookies(default_domain=default_domain)
            await context.clear_cookies()
            await context.add_cookies(cookies)
            logger.info(
                "Применено %s cookie из сессии %s", len(cookies), session.name
            )
            return session

        # Если сессии ещё не загружены — инициализируем без сетевой валидации,
        # чтобы не блокировать старт при временных сетевых сбоях.
        if self._cookie_manager.active_session is None:
            try:
                await self._cookie_manager.initialize(validate=False)
            except Exception as exc:
                logger.error(
                    "Не удалось загрузить cookie для браузера: %s",
                    exc,
                    exc_info=True,
                )
                raise BrowserEngineError(
                    f"Не удалось применить cookie: {exc}"
                ) from exc

        return await self._cookie_manager.apply_to_playwright(context)

    # ------------------------------------------------------------------
    # Навигация
    # ------------------------------------------------------------------

    async def goto_with_retry(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        retries: Optional[int] = None,
        timeout_ms: Optional[int] = None,
        wait_selector: Optional[str] = None,
    ) -> Page:
        """
        Безопасная навигация с повторными попытками при сетевых таймаутах.

        Raises:
            NavigationError: если все попытки исчерпаны
        """
        page = self.page
        max_retries = retries if retries is not None else self._browser_cfg.goto_retries
        timeout = (
            timeout_ms
            if timeout_ms is not None
            else self._browser_cfg.navigation_timeout_ms
        )
        last_error: Optional[BaseException] = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.info(
                    "Переход на %s (попытка %s/%s)", url, attempt, max_retries
                )
                await self._human_delay("navigation")
                response = await page.goto(
                    url,
                    wait_until=wait_until,  # type: ignore[arg-type]
                    timeout=timeout,
                )
                if response is not None and response.status >= 500:
                    raise NavigationError(
                        f"Сервер вернул HTTP {response.status} для {url}"
                    )

                if wait_selector:
                    await page.wait_for_selector(
                        wait_selector,
                        timeout=self._browser_cfg.default_timeout_ms,
                        state="attached",
                    )

                await self._patch_webdriver(page)
                logger.info("Навигация успешна: %s -> %s", url, page.url)
                return page

            except PlaywrightTimeoutError as exc:
                last_error = exc
                logger.warning(
                    "Таймаут навигации на %s (попытка %s/%s): %s",
                    url,
                    attempt,
                    max_retries,
                    exc,
                )
                await self.capture_error_screenshot(prefix=f"goto_timeout_{attempt}")
            except PlaywrightError as exc:
                last_error = exc
                message = str(exc).lower()
                is_network = any(
                    token in message
                    for token in (
                        "net::",
                        "timeout",
                        "err_connection",
                        "err_name_not_resolved",
                        "err_internet_disconnected",
                        "err_tunnel",
                        "navigation",
                    )
                )
                if not is_network:
                    await self.capture_error_screenshot(prefix="goto_fatal")
                    raise NavigationError(f"Ошибка навигации на {url}: {exc}") from exc

                logger.warning(
                    "Сетевая ошибка навигации на %s (попытка %s/%s): %s",
                    url,
                    attempt,
                    max_retries,
                    exc,
                )
                await self.capture_error_screenshot(prefix=f"goto_network_{attempt}")
            except Exception as exc:
                last_error = exc
                logger.error(
                    "Неожиданная ошибка навигации на %s: %s",
                    url,
                    exc,
                    exc_info=True,
                )
                await self.capture_error_screenshot(prefix="goto_unexpected")
                raise NavigationError(f"Ошибка навигации на {url}: {exc}") from exc

            if attempt < max_retries:
                backoff = self._browser_cfg.goto_retry_backoff_sec * attempt
                jitter = random.uniform(0.2, 1.0)
                await asyncio.sleep(backoff + jitter)

        raise NavigationError(
            f"Не удалось открыть {url} после {max_retries} попыток: {last_error}"
        )

    async def open_game(self) -> Page:
        """Открывает входную точку игры с ожиданием авторизованных фреймов."""
        selectors = self._config.selectors
        wait_selector = (
            f"{selectors.frameset}, {selectors.main_frame}, {selectors.logged_in_marker}"
        )
        return await self.goto_with_retry(
            self._config.server.game_entry_url,
            wait_until="domcontentloaded",
            wait_selector=wait_selector,
        )

    # ------------------------------------------------------------------
    # Human-like взаимодействие
    # ------------------------------------------------------------------

    async def human_click(
        self,
        target: SelectorLike,
        *,
        page: Optional[Page] = None,
        frame: Optional[Frame] = None,
        timeout_ms: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        force: bool = False,
    ) -> None:
        """
        Клик с имитацией человека:
          1) ожидание элемента;
          2) случайная пауза / rare idle;
          3) наведение по кривой Безье (HumanBehavior);
          4) короткая пауза pre_click;
          5) mouse down/up с микро-задержкой.
        """
        owner: Union[Page, Frame]
        if frame is not None:
            owner = frame
        else:
            owner = page or self.page

        page_ref = page or self.page
        timeout = (
            timeout_ms
            if timeout_ms is not None
            else self._browser_cfg.default_timeout_ms
        )

        try:
            selector_str = target if isinstance(target, str) else ""
            if isinstance(target, str):
                await owner.wait_for_selector(
                    target, timeout=timeout, state="visible"
                )
                element = await owner.query_selector(target)
                if element is None:
                    raise BrowserEngineError(f"Элемент не найден: {target}")
            else:
                element = target
                visible = await element.is_visible()
                if not visible and not force:
                    raise BrowserEngineError("Элемент для клика не видим")

            await self._human_delay("click")
            # Редкая «задумчивость» перед важным кликом
            await self._human.random_idle(page_ref, chance=0.04)

            box = await element.bounding_box()
            if box is None:
                logger.debug(
                    "bounding_box недоступен, используем element.click()"
                )
                await element.click(
                    button=button,  # type: ignore[arg-type]
                    click_count=click_count,
                    force=force,
                    timeout=timeout,
                )
                return

            try:
                if selector_str:
                    end = await self._human.bezier_mouse_move(
                        page_ref,
                        selector_str,
                        frame=frame,
                        element=element,
                        timeout_ms=timeout,
                    )
                    x, y = end
                else:
                    dx, dy = self._delays.click_offset()
                    margin_x = max(1.0, box["width"] * 0.1)
                    margin_y = max(1.0, box["height"] * 0.1)
                    x = box["x"] + box["width"] / 2 + dx
                    y = box["y"] + box["height"] / 2 + dy
                    x = min(
                        max(x, box["x"] + margin_x),
                        box["x"] + box["width"] - margin_x,
                    )
                    y = min(
                        max(y, box["y"] + margin_y),
                        box["y"] + box["height"] - margin_y,
                    )
                    await self._human.move_along_bezier(page_ref, (x, y))
            except Exception as bezier_exc:
                logger.debug(
                    "Bezier move fallback to linear: %s", bezier_exc
                )
                dx, dy = self._delays.click_offset()
                x = box["x"] + box["width"] / 2 + dx
                y = box["y"] + box["height"] / 2 + dy
                await page_ref.mouse.move(x, y, steps=random.randint(5, 18))
                self._human.set_cursor(x, y)

            await self._human_delay("pre_click")

            for _ in range(click_count):
                await page_ref.mouse.down(button=button)  # type: ignore[arg-type]
                await asyncio.sleep(random.uniform(0.04, 0.12))
                await page_ref.mouse.up(button=button)  # type: ignore[arg-type]
                if click_count > 1:
                    await asyncio.sleep(random.uniform(0.05, 0.15))

            logger.debug("human_click (bezier): (%.1f, %.1f)", x, y)
        except PlaywrightTimeoutError as exc:
            await self.capture_error_screenshot(prefix="human_click_timeout")
            raise BrowserEngineError(
                f"Таймаут ожидания элемента для клика: {target!r}"
            ) from exc
        except BrowserEngineError:
            raise
        except Exception as exc:
            await self.capture_error_screenshot(prefix="human_click_error")
            logger.error("Ошибка human_click: %s", exc, exc_info=True)
            raise BrowserEngineError(f"human_click не удался: {exc}") from exc

    async def human_type(
        self,
        selector: str,
        text: str,
        *,
        page: Optional[Page] = None,
        frame: Optional[Frame] = None,
        clear_first: bool = True,
        typo_chance: float = 0.05,
    ) -> None:
        """
        Human-like набор текста через HumanBehavior.human_type
        (разные задержки, опечатки ~5%).
        """
        page_ref = page or self.page
        try:
            await self._human.random_idle(page_ref, chance=0.06)
            await self._human.human_type(
                page_ref,
                selector,
                text,
                frame=frame,
                clear_first=clear_first,
                typo_chance=typo_chance,
            )
        except Exception as exc:
            await self.capture_error_screenshot(prefix="human_type_error")
            logger.error("Ошибка human_type: %s", exc, exc_info=True)
            raise BrowserEngineError(f"human_type не удался: {exc}") from exc

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout_ms: Optional[int] = None,
        state: str = "visible",
        page: Optional[Page] = None,
        frame: Optional[Frame] = None,
    ) -> Optional[ElementHandle]:
        """Ожидание селектора с обработкой TimeoutError."""
        owner: Union[Page, Frame] = frame if frame is not None else (page or self.page)
        timeout = (
            timeout_ms
            if timeout_ms is not None
            else self._browser_cfg.default_timeout_ms
        )
        try:
            handle = await owner.wait_for_selector(
                selector,
                timeout=timeout,
                state=state,  # type: ignore[arg-type]
            )
            return handle
        except PlaywrightTimeoutError:
            logger.warning(
                "Селектор не появился за %s мс: %s", timeout, selector
            )
            return None

    async def get_frame(self, selector: str) -> Optional[Frame]:
        """
        Возвращает Frame по CSS-селектору frame/iframe.

        Ищет среди page.frames по name/url и через element_handle.content_frame().
        """
        page = self.page
        # Попытка через DOM-элемент
        try:
            element = await page.query_selector(selector)
            if element is not None:
                content = await element.content_frame()
                if content is not None:
                    return content
        except PlaywrightError as exc:
            logger.debug("get_frame через selector '%s': %s", selector, exc)

        # Fallback: разбор селектора на name=...
        name_match = None
        if "name='" in selector or 'name="' in selector:
            for part in selector.replace('"', "'").split(","):
                part = part.strip()
                if "name='" in part:
                    name_match = part.split("name='", 1)[1].split("'", 1)[0]
                    break

        if name_match:
            for frame in page.frames:
                if frame.name == name_match:
                    return frame

        return None

    async def get_main_frame(self) -> Optional[Frame]:
        return await self.get_frame(self._config.selectors.main_frame)

    async def get_combat_frame(self) -> Optional[Frame]:
        return await self.get_frame(self._config.selectors.combat_frame)

    async def get_backpack_frame(self) -> Optional[Frame]:
        return await self.get_frame(self._config.selectors.backpack_frame)

    async def get_chat_frame(self) -> Optional[Frame]:
        return await self.get_frame(self._config.selectors.chat_frame)

    async def get_navigation_frame(self) -> Optional[Frame]:
        return await self.get_frame(self._config.selectors.navigation_frame)

    # ------------------------------------------------------------------
    # Скриншоты / сеть
    # ------------------------------------------------------------------

    async def capture_error_screenshot(self, prefix: str = "error") -> Optional[Path]:
        """Сохраняет скриншот при ошибке, если флаг screenshot_on_error включён."""
        if not self._browser_cfg.screenshot_on_error:
            return None
        if self._page is None or self._page.is_closed():
            return None

        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = SCREENSHOTS_DIR / f"{prefix}_{stamp}.png"
        try:
            await self._page.screenshot(path=str(path), full_page=True)
            logger.info("Скриншот ошибки сохранён: %s", path)
            return path
        except Exception as exc:
            logger.warning("Не удалось сделать скриншот: %s", exc, exc_info=True)
            return None

    def _attach_network_listeners(self, page: Page) -> None:
        """Перехват сетевых запросов (логирование URL и статусов)."""

        def on_request(request: Any) -> None:
            try:
                entry = {
                    "type": "request",
                    "method": request.method,
                    "url": request.url,
                    "resource_type": request.resource_type,
                }
                self._network_log.append(entry)
                if len(self._network_log) > 500:
                    self._network_log = self._network_log[-300:]
                logger.debug("NET → %s %s", request.method, request.url)
            except Exception:
                logger.debug("Ошибка обработчика request", exc_info=True)

        def on_response(response: Any) -> None:
            try:
                entry = {
                    "type": "response",
                    "status": response.status,
                    "url": response.url,
                }
                self._network_log.append(entry)
                if response.status >= 400:
                    logger.warning("NET ← %s %s", response.status, response.url)
                else:
                    logger.debug("NET ← %s %s", response.status, response.url)
            except Exception:
                logger.debug("Ошибка обработчика response", exc_info=True)

        def on_request_failed(request: Any) -> None:
            try:
                failure = request.failure
                logger.warning(
                    "NET ✕ failed %s %s (%s)",
                    request.method,
                    request.url,
                    failure,
                )
            except Exception:
                logger.debug("Ошибка обработчика requestfailed", exc_info=True)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    def recent_network_log(self, limit: int = 50) -> Sequence[Dict[str, Any]]:
        return tuple(self._network_log[-limit:])

    # ------------------------------------------------------------------
    # Внутренние утилиты
    # ------------------------------------------------------------------

    async def _human_delay(self, kind: str) -> None:
        delay = self._delays.uniform(kind)
        logger.debug("Human delay (%s): %.3f сек", kind, delay)
        await asyncio.sleep(delay)

    async def _patch_webdriver(self, page: Page) -> None:
        try:
            await page.evaluate(
                """() => {
                    try {
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                            configurable: true,
                        });
                    } catch (e) {}
                    return navigator.webdriver;
                }"""
            )
        except PlaywrightError as exc:
            logger.debug("Не удалось пропатчить webdriver: %s", exc)

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Безопасный page.evaluate с логированием ошибок."""
        try:
            if arg is None:
                return await self.page.evaluate(expression)
            return await self.page.evaluate(expression, arg)
        except PlaywrightError as exc:
            logger.error("evaluate failed: %s\n%s", exc, traceback.format_exc())
            await self.capture_error_screenshot(prefix="evaluate_error")
            raise BrowserEngineError(f"evaluate failed: {exc}") from exc
