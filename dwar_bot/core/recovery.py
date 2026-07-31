"""
Авто-восстановление после зависаний, сетевых сбоев и падения Playwright.

CrashRecoveryManager проверяет «живость» страницы, умеет полностью
пересоздать BrowserContext с перезагрузкой cookies.json и оборачивает
критические вызовы в retry с экспоненциальной задержкой.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, TypeVar

from playwright.async_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError

from dwar_bot.auth.cookie_manager import CookieManager
from dwar_bot.config import BotConfig, config
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError, NavigationError

logger = logging.getLogger(__name__)

T = TypeVar("T")

RE_HTTP_ERROR = re.compile(
    r"(?:502\s*bad\s*gateway|504\s*gateway\s*time|503\s*service|"
    r"500\s*internal|err_connection|err_tunnel|error\s*code|"
    r"страница\s+недоступна|сервер\s+временно)",
    re.IGNORECASE,
)
RE_BLANK_HINTS = re.compile(
    r"(?:about:blank|chrome-error://|data:text/html,\s*$)",
    re.IGNORECASE,
)

# Ошибки, при которых имеет смысл retry / restart
RECOVERABLE_EXCEPTIONS = (
    PlaywrightTimeoutError,
    PlaywrightError,
    BrowserEngineError,
    NavigationError,
    TimeoutError,
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
)


class CrashRecoveryManager:
    """Менеджер health-check и аварийного перезапуска сессии."""

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        health_timeout_ms: int = 12_000,
        max_consecutive_restarts: int = 5,
        restart_cooldown_sec: float = 15.0,
    ) -> None:
        self._config = bot_config or config
        self._health_timeout_ms = health_timeout_ms
        self._max_consecutive_restarts = max_consecutive_restarts
        self._restart_cooldown_sec = restart_cooldown_sec
        self._consecutive_restarts = 0
        self._last_restart_at = 0.0
        self._last_health_ok = True

    @property
    def consecutive_restarts(self) -> int:
        return self._consecutive_restarts

    def reset_restart_counter(self) -> None:
        self._consecutive_restarts = 0

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self, page: Page) -> bool:
        """
        Проверяет отзывчивость страницы игры.

        False если:
          - page закрыт / about:blank;
          - HTTP 502/504/500 в title/body;
          - белый пустой экран без фреймов и контента;
          - главные iframe/frameset не загружены при ожидании игры.
        """
        try:
            if page.is_closed():
                logger.warning("health_check: page закрыт")
                self._last_health_ok = False
                return False

            url = (page.url or "").strip()
            if not url or RE_BLANK_HINTS.search(url):
                logger.warning("health_check: blank URL=%r", url)
                self._last_health_ok = False
                return False

            # Быстрый evaluate — страница должна отвечать JS
            try:
                ready = await asyncio.wait_for(
                    page.evaluate(
                        """() => ({
                            ready: document.readyState,
                            bodyLen: (document.body && document.body.innerText || '').length,
                            title: document.title || '',
                            frames: window.frames ? window.frames.length : 0,
                            hasFrameset: !!document.querySelector('frameset, iframe, frame')
                        })"""
                    ),
                    timeout=self._health_timeout_ms / 1000.0,
                )
            except (asyncio.TimeoutError, PlaywrightError) as exc:
                logger.warning("health_check: page.evaluate timeout/error: %s", exc)
                self._last_health_ok = False
                return False

            title = str(ready.get("title") or "")
            body_len = int(ready.get("bodyLen") or 0)
            frames = int(ready.get("frames") or 0)
            has_frameset = bool(ready.get("hasFrameset"))

            if RE_HTTP_ERROR.search(title):
                logger.warning("health_check: error in title: %s", title[:120])
                self._last_health_ok = False
                return False

            # Проверка HTML на 502/504
            try:
                content_snip = await asyncio.wait_for(
                    page.content(),
                    timeout=self._health_timeout_ms / 1000.0,
                )
            except (asyncio.TimeoutError, PlaywrightError) as exc:
                logger.warning("health_check: content() failed: %s", exc)
                self._last_health_ok = False
                return False

            if RE_HTTP_ERROR.search(content_snip[:4000]):
                logger.warning("health_check: HTTP error page detected")
                self._last_health_ok = False
                return False

            # Белый экран: почти нет текста и нет фреймов
            if body_len < 15 and frames == 0 and not has_frameset:
                # На лендинге dwar.ru может быть мало текста — проверяем URL
                if "dwar.ru" in url and ("game" in url or "user" in url):
                    logger.warning(
                        "health_check: похоже на белый экран (body=%s frames=%s)",
                        body_len,
                        frames,
                    )
                    self._last_health_ok = False
                    return False

            # Ожидаем игровые фреймы на game.php
            if "game.php" in url.lower() or "game" in url.lower():
                selectors = self._config.selectors
                frame_ok = await self._any_selector_present(
                    page,
                    (
                        selectors.frameset,
                        selectors.main_frame,
                        selectors.game_iframe,
                        selectors.logged_in_marker,
                    ),
                )
                if not frame_ok and frames == 0 and not has_frameset:
                    logger.warning("health_check: игровые iframe не найдены")
                    self._last_health_ok = False
                    return False

            self._last_health_ok = True
            if self._consecutive_restarts > 0:
                # Успешный health после рестартов — сбрасываем счётчик постепенно
                self._consecutive_restarts = max(0, self._consecutive_restarts - 1)
            return True

        except Exception as exc:
            logger.error("health_check unexpected: %s", exc, exc_info=True)
            self._last_health_ok = False
            return False

    async def _any_selector_present(
        self, page: Page, selectors: tuple[str, ...]
    ) -> bool:
        for group in selectors:
            for selector in [p.strip() for p in group.split(",") if p.strip()]:
                try:
                    handle = await page.query_selector(selector)
                    if handle is not None:
                        return True
                except PlaywrightError:
                    continue
        # Также по именам фреймов
        try:
            for frame in page.frames:
                name = (frame.name or "").lower()
                if name in {"main", "fight", "combat", "menu", "chat"}:
                    return True
        except PlaywrightError:
            pass
        return False

    # ------------------------------------------------------------------
    # Restart session
    # ------------------------------------------------------------------

    async def restart_session(
        self,
        browser_engine: BrowserEngine,
        cookie_manager: CookieManager,
        *,
        open_game: bool = True,
    ) -> Page:
        """
        Полный перезапуск Playwright-сессии:
          stop → reload cookies.json → start → goto game URL.
        """
        now = time.monotonic()
        since_last = now - self._last_restart_at
        if since_last < self._restart_cooldown_sec:
            wait = self._restart_cooldown_sec - since_last
            logger.info("restart_session cooldown: ждём %.1f сек", wait)
            await asyncio.sleep(wait)

        self._consecutive_restarts += 1
        self._last_restart_at = time.monotonic()

        if self._consecutive_restarts > self._max_consecutive_restarts:
            raise BrowserEngineError(
                f"Слишком много рестартов подряд ({self._consecutive_restarts}). "
                "Требуется ручное вмешательство."
            )

        logger.critical(
            "CrashRecovery: перезапуск сессии (#%s)",
            self._consecutive_restarts,
        )

        try:
            await browser_engine.stop()
        except Exception as exc:
            logger.warning("Ошибка при stop() перед рестартом: %s", exc)

        # Небольшая пауза — дать ОС освободить ресурсы браузера
        await asyncio.sleep(random.uniform(1.0, 2.5))

        # Перечитываем cookies.json
        try:
            cookies_file = cookie_manager.cookies_file
            if cookies_file.is_file():
                cookie_manager.load_cookies(cookies_file, validate_structure=True)
                logger.info("Cookie перезагружены из %s", cookies_file)
            else:
                cookie_manager.load_all_sessions()
                logger.warning(
                    "cookies.json отсутствует — используем найденные сессии (%s)",
                    len(cookie_manager.sessions),
                )
        except Exception as exc:
            logger.error(
                "Не удалось перезагрузить cookie: %s — пробуем start всё равно",
                exc,
                exc_info=True,
            )

        page = await browser_engine.start(apply_cookies=True)

        if open_game:
            try:
                await browser_engine.goto_with_retry(
                    self._config.server.game_entry_url,
                    wait_until="domcontentloaded",
                )
            except Exception as exc:
                logger.warning(
                    "goto game_entry после рестарта не удался (%s) — open_game()",
                    exc,
                )
                await browser_engine.open_game()

        # Проверка после рестарта
        ok = await self.health_check(page)
        if not ok:
            logger.error("health_check после restart_session всё ещё FAIL")
        else:
            logger.info("Сессия успешно восстановлена")

        return page

    async def ensure_healthy(
        self,
        browser_engine: BrowserEngine,
        cookie_manager: CookieManager,
    ) -> bool:
        """
        Health-check; при провале — restart_session.

        Returns:
            True если страница здорова (возможно после рестарта).
        """
        if not browser_engine.is_started:
            logger.warning("ensure_healthy: браузер не запущен — стартуем")
            await self.restart_session(browser_engine, cookie_manager)
            return await self.health_check(browser_engine.page)

        try:
            page = browser_engine.page
        except BrowserEngineError:
            await self.restart_session(browser_engine, cookie_manager)
            return await self.health_check(browser_engine.page)

        if await self.health_check(page):
            return True

        logger.warning("ensure_healthy: health FAIL — restart_session")
        await self.restart_session(browser_engine, cookie_manager)
        return await self.health_check(browser_engine.page)

    # ------------------------------------------------------------------
    # safe_execute
    # ------------------------------------------------------------------

    async def safe_execute(
        self,
        async_func: Callable[..., Awaitable[T]],
        *args: Any,
        max_retries: int = 3,
        base_delay: float = 1.0,
        retry_on: tuple[type[BaseException], ...] = RECOVERABLE_EXCEPTIONS,
        on_retry: Optional[Callable[[int, BaseException], Awaitable[None]]] = None,
        **kwargs: Any,
    ) -> T:
        """
        Вызов критической async-функции с retry и экспоненциальной задержкой.

        delay = base_delay * (2 ** attempt) + jitter
        """
        last_exc: Optional[BaseException] = None
        attempts = max(1, int(max_retries))

        for attempt in range(attempts):
            try:
                result = await async_func(*args, **kwargs)
                return result
            except retry_on as exc:
                last_exc = exc
                if attempt >= attempts - 1:
                    break
                delay = base_delay * (2 ** attempt) + random.uniform(0.1, 0.6)
                logger.warning(
                    "safe_execute: %s failed (attempt %s/%s): %s — sleep %.1fs",
                    getattr(async_func, "__name__", repr(async_func)),
                    attempt + 1,
                    attempts,
                    exc,
                    delay,
                )
                if on_retry is not None:
                    try:
                        await on_retry(attempt + 1, exc)
                    except Exception as hook_exc:
                        logger.debug("on_retry hook error: %s", hook_exc)
                await asyncio.sleep(delay)
            except Exception as exc:
                # Неретраибельная ошибка
                logger.error(
                    "safe_execute: non-recoverable error in %s: %s",
                    getattr(async_func, "__name__", repr(async_func)),
                    exc,
                    exc_info=True,
                )
                raise

        assert last_exc is not None
        logger.error(
            "safe_execute: исчерпаны %s попыток для %s",
            attempts,
            getattr(async_func, "__name__", repr(async_func)),
        )
        raise last_exc

    def wrap(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
        """Декоратор-обёртка над ``safe_execute``."""

        def decorator(
            async_func: Callable[..., Awaitable[T]],
        ) -> Callable[..., Awaitable[T]]:
            @wraps(async_func)
            async def wrapper(*args: Any, **kwargs: Any) -> T:
                return await self.safe_execute(
                    async_func,
                    *args,
                    max_retries=max_retries,
                    base_delay=base_delay,
                    **kwargs,
                )

            return wrapper

        return decorator
