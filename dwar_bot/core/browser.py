"""Обёртка над Playwright (async) для управления браузером в игре Dwar.

Ответственность:
    * запуск браузера с anti-detection настройками (маскировка ``navigator.webdriver``
      и т.п.);
    * создание контекста с нужными UA/locale/viewport/proxy;
    * применение куки через :class:`CookieManager`;
    * перехват сетевых запросов/ответов (для реакции на состояние игры и
      блокировки лишних ресурсов);
    * безопасные обёртки над навигацией и поиском элементов с обработкой
      ``TimeoutError`` и повторными попытками.

Все методы асинхронные. Класс поддерживает протокол ``async with``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from ..auth.cookie_manager import CookieManager, NoSessionsAvailableError
from ..config import CONFIG
from ..logger import get_logger, log_exception

logger = get_logger(__name__)


# JS-скрипт, снижающий вероятность детекта автоматизации.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
"""

# Типы ресурсов, которые блокируем для скорости и снижения шума.
_BLOCKED_RESOURCE_TYPES = {"font", "media"}


class BrowserManager:
    """Управляет жизненным циклом Playwright, контекста и страницы."""

    def __init__(self, cookie_manager: Optional[CookieManager] = None) -> None:
        self._cookie_manager = cookie_manager or CookieManager()
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        # Хранилище последних сетевых ответов игры (url -> метаданные).
        self._network_log: List[Dict] = []
        self._network_log_limit = 200
        # Пользовательские обработчики ответов: url_substring -> callback(response)
        self._response_hooks: Dict[str, Callable[[Response], None]] = {}

    # ------------------------------------------------------------------ #
    #  Свойства                                                           #
    # ------------------------------------------------------------------ #
    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Браузер не запущен: page недоступен")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Браузер не запущен: context недоступен")
        return self._context

    @property
    def cookie_manager(self) -> CookieManager:
        return self._cookie_manager

    @property
    def network_log(self) -> List[Dict]:
        return list(self._network_log)

    # ------------------------------------------------------------------ #
    #  Жизненный цикл                                                     #
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Запускает Playwright, браузер, контекст и страницу."""
        cfg = CONFIG.browser
        logger.info(
            "Запуск браузера: engine=%s headless=%s", cfg.engine, cfg.headless
        )
        self._playwright = await async_playwright().start()

        engine = getattr(self._playwright, cfg.engine, None)
        if engine is None:
            logger.warning(
                "Неизвестный движок '%s', использую chromium", cfg.engine
            )
            engine = self._playwright.chromium

        launch_args = self._build_launch_args()
        proxy = self._build_proxy()

        context_kwargs = self._build_context_kwargs()

        # Persistent-контекст (если задан user_data_dir) удобен для сохранения
        # сессии между запусками; иначе — обычный контекст поверх Browser.
        if cfg.user_data_dir:
            Path(cfg.user_data_dir).mkdir(parents=True, exist_ok=True)
            self._context = await engine.launch_persistent_context(
                cfg.user_data_dir,
                headless=cfg.headless,
                slow_mo=cfg.slow_mo_ms,
                args=launch_args,
                proxy=proxy,
                **context_kwargs,
            )
            self._browser = None
        else:
            self._browser = await engine.launch(
                headless=cfg.headless,
                slow_mo=cfg.slow_mo_ms,
                args=launch_args,
                proxy=proxy,
            )
            self._context = await self._browser.new_context(**context_kwargs)

        self._context.set_default_timeout(cfg.default_timeout_ms)
        self._context.set_default_navigation_timeout(cfg.navigation_timeout_ms)

        # Anti-detection скрипт на каждую новую страницу.
        await self._context.add_init_script(_STEALTH_JS)

        # Берём первую страницу persistent-контекста либо создаём новую.
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        await self._setup_network_interception()
        logger.info("Браузер и контекст готовы")

    async def apply_session(self, rotate: bool = False):
        """Применяет куки к контексту. При ``rotate`` берёт следующую сессию."""
        if rotate:
            self._cookie_manager.invalidate_current()
        session = self._cookie_manager.next_session() if rotate else None
        return await self._cookie_manager.apply_to_context(self._context, session)

    async def close(self) -> None:
        """Аккуратно закрывает все ресурсы Playwright."""
        for closer in (
            self._close_context,
            self._close_browser,
            self._stop_playwright,
        ):
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001
                log_exception(logger, "Ошибка при закрытии браузера", exc)
        logger.info("Браузер закрыт")

    async def _close_context(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
            self._page = None

    async def _close_browser(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None

    async def _stop_playwright(self) -> None:
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    #  Навигация и поиск элементов                                        #
    # ------------------------------------------------------------------ #
    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> Optional[Response]:
        """Навигация с повторными попытками и обработкой таймаута."""
        retries = CONFIG.runtime.max_retries
        backoff = CONFIG.runtime.retry_backoff_base
        last_exc: Optional[BaseException] = None

        for attempt in range(1, retries + 1):
            try:
                logger.debug("Навигация (%d/%d) -> %s", attempt, retries, url)
                response = await self.page.goto(url, wait_until=wait_until)
                return response
            except PlaywrightTimeoutError as exc:
                last_exc = exc
                logger.warning(
                    "Таймаут навигации на %s (попытка %d/%d)", url, attempt, retries
                )
            except PlaywrightError as exc:
                last_exc = exc
                logger.warning(
                    "Ошибка навигации на %s (попытка %d/%d): %s",
                    url,
                    attempt,
                    retries,
                    exc,
                )
            if attempt < retries:
                await asyncio.sleep(backoff * attempt)

        if last_exc is not None:
            log_exception(logger, f"Не удалось перейти на {url}", last_exc)
        return None

    async def wait_for_selector(
        self,
        selector: str,
        timeout_ms: Optional[int] = None,
        state: str = "visible",
    ):
        """Ожидание селектора. Возвращает ElementHandle или None при таймауте."""
        try:
            return await self.page.wait_for_selector(
                selector,
                timeout=timeout_ms or CONFIG.browser.default_timeout_ms,
                state=state,
            )
        except PlaywrightTimeoutError:
            logger.debug("Селектор не найден за отведённое время: %s", selector)
            return None
        except PlaywrightError as exc:
            log_exception(logger, f"Ошибка ожидания селектора {selector}", exc)
            return None

    async def query_text(self, selector: str, default: str = "") -> str:
        """Возвращает текст первого совпадения селектора (или default)."""
        try:
            element = await self.page.query_selector(selector)
            if element is None:
                return default
            text = await element.inner_text()
            return text.strip()
        except PlaywrightError as exc:
            logger.debug("query_text(%s) ошибка: %s", selector, exc)
            return default

    async def query_all_texts(self, selector: str) -> List[str]:
        """Возвращает тексты всех совпадений селектора."""
        results: List[str] = []
        try:
            elements = await self.page.query_selector_all(selector)
            for element in elements:
                try:
                    text = (await element.inner_text()).strip()
                    if text:
                        results.append(text)
                except PlaywrightError:
                    continue
        except PlaywrightError as exc:
            logger.debug("query_all_texts(%s) ошибка: %s", selector, exc)
        return results

    async def exists(self, selector: str, timeout_ms: int = 1500) -> bool:
        """Проверяет наличие видимого элемента без длинного ожидания."""
        element = await self.wait_for_selector(selector, timeout_ms=timeout_ms)
        return element is not None

    async def screenshot(self, tag: str = "error") -> Optional[Path]:
        """Делает скриншот страницы для диагностики."""
        if self._page is None:
            return None
        try:
            CONFIG.runtime.screenshots_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{tag}_{int(time.time())}.png"
            path = CONFIG.runtime.screenshots_dir / filename
            await self._page.screenshot(path=str(path), full_page=True)
            logger.info("Скриншот сохранён: %s", path)
            return path
        except PlaywrightError as exc:
            log_exception(logger, "Не удалось сделать скриншот", exc)
            return None

    # ------------------------------------------------------------------ #
    #  Проверка авторизации                                               #
    # ------------------------------------------------------------------ #
    async def is_logged_in(self) -> bool:
        """Определяет, находимся ли мы в авторизованной зоне игры."""
        marker = CONFIG.selectors.logged_in_marker
        login_form = CONFIG.selectors.login_form
        # Признак разлогина — наличие формы входа.
        if await self.exists(login_form, timeout_ms=1200):
            return False
        return await self.exists(marker, timeout_ms=2500)

    async def ensure_authorized(self) -> bool:
        """Гарантирует авторизацию: применяет куки, при провале — ротация.

        Возвращает ``True``, если удалось авторизоваться хотя бы одной сессией.
        """
        cfg = CONFIG.game
        attempts = 0
        max_attempts = max(1, len(self._cookie_manager.discover_sessions()))

        while attempts < max_attempts:
            attempts += 1
            try:
                await self._cookie_manager.apply_to_context(
                    self._context,
                    self._cookie_manager.next_session(),
                )
            except NoSessionsAvailableError as exc:
                log_exception(logger, "Нет доступных сессий для авторизации", exc)
                return False

            await self.goto(cfg.main_url)
            if await self.is_logged_in():
                logger.info("Авторизация успешна")
                return True

            logger.warning(
                "Сессия недействительна на сайте, пробую следующую (%d/%d)",
                attempts,
                max_attempts,
            )
            self._cookie_manager.invalidate_current()

        logger.error("Не удалось авторизоваться ни одной сессией")
        return False

    # ------------------------------------------------------------------ #
    #  Сетевой перехват                                                   #
    # ------------------------------------------------------------------ #
    def add_response_hook(self, url_substring: str, callback: Callable[[Response], None]) -> None:
        """Регистрирует обработчик ответов, чьи URL содержат подстроку."""
        self._response_hooks[url_substring] = callback

    async def _setup_network_interception(self) -> None:
        """Настраивает роутинг (блокировка ресурсов) и слушатели ответов."""
        assert self._context is not None

        async def _route_handler(route):
            try:
                if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            except PlaywrightError:
                # Роут мог быть уже обработан/закрыт.
                pass

        try:
            await self._context.route("**/*", _route_handler)
        except PlaywrightError as exc:
            logger.debug("Не удалось установить route-handler: %s", exc)

        self._context.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        """Слушатель ответов: пишет в лог и вызывает пользовательские хуки."""
        try:
            entry = {
                "url": response.url,
                "status": response.status,
                "ok": response.ok,
                "ts": time.time(),
            }
            self._network_log.append(entry)
            if len(self._network_log) > self._network_log_limit:
                self._network_log.pop(0)

            for substring, callback in self._response_hooks.items():
                if substring in response.url:
                    try:
                        callback(response)
                    except Exception as exc:  # noqa: BLE001
                        log_exception(logger, "Ошибка в response-hook", exc)
        except PlaywrightError:
            pass

    # ------------------------------------------------------------------ #
    #  Вспомогательные билдеры конфигурации                               #
    # ------------------------------------------------------------------ #
    def _build_launch_args(self) -> List[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
        ]
        return args

    def _build_proxy(self) -> Optional[Dict[str, str]]:
        raw = CONFIG.browser.proxy.strip()
        if not raw:
            return None
        # Формат: scheme://user:pass@host:port -> Playwright proxy dict.
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        if not parsed.hostname:
            logger.warning("Некорректный формат прокси: %s", raw)
            return None
        server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        proxy: Dict[str, str] = {"server": server}
        if parsed.username:
            proxy["username"] = parsed.username
        if parsed.password:
            proxy["password"] = parsed.password
        return proxy

    def _build_context_kwargs(self) -> Dict:
        cfg = CONFIG.browser
        kwargs: Dict = {
            "locale": cfg.locale,
            "timezone_id": cfg.timezone_id,
            "viewport": {
                "width": cfg.viewport_width,
                "height": cfg.viewport_height,
            },
        }
        if cfg.user_agent:
            kwargs["user_agent"] = cfg.user_agent
        return kwargs


__all__ = ["BrowserManager"]
