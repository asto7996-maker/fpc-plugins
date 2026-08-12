"""
Playwright browser factory with anti-detection fingerprinting.

Creates a persistent browser context that:
* Overrides navigator.webdriver and related automation fingerprints
* Sets realistic geolocation, locale, timezone, and screen dimensions
* Intercepts and optionally logs network requests
* Provides a pre-warmed Page ready for game interaction
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Request,
    Response,
    async_playwright,
)

from dwar_bot.config import (
    BROWSER_EXECUTABLE_PATH,
    BROWSER_LAUNCH_ARGS,
    EXTRA_HTTP_HEADERS,
    HEADLESS,
    LOCALE,
    NAVIGATION_TIMEOUT_MS,
    NETWORK_WATCH_PATTERNS,
    PAGE_TIMEOUT_MS,
    SCREENSHOTS_DIR,
    SCREENSHOT_ON_ERROR,
    TIMEZONE,
    USER_AGENT,
    VIEWPORT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JS snippet injected into every page to mask Playwright fingerprints
# ---------------------------------------------------------------------------
_STEALTH_INIT_SCRIPT = """
(() => {
    // Remove webdriver property
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Realistic plugin list
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ],
    });

    // Realistic language list
    Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });

    // Hardware concurrency (pretend quad-core)
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });

    // Device memory
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // Platform
    Object.defineProperty(navigator, 'platform', { get: () => 'Linux x86_64' });

    // Permissions query — avoid returning 'denied' for notifications (suspicious)
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);

    // Spoof chrome object
    if (!window.chrome) {
        window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };
    }

    // Canvas noise — slight per-session pixel offset
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const imageData = ctx.getImageData(0, 0, this.width, this.height);
            imageData.data[0] = imageData.data[0] ^ (Math.floor(Math.random() * 3));
            ctx.putImageData(imageData, 0, 0);
        }
        return _toDataURL.apply(this, arguments);
    };
})();
"""

# ---------------------------------------------------------------------------
# BrowserManager
# ---------------------------------------------------------------------------

class BrowserManager:
    """
    Lifecycle manager for a single Playwright browser + context + page.

    Usage::

        async with BrowserManager() as bm:
            page = await bm.new_page()
            await page.goto("https://dwar.ru")
    """

    def __init__(
        self,
        headless: bool = HEADLESS,
        executable_path: Optional[str] = BROWSER_EXECUTABLE_PATH,
        user_data_dir: Optional[Path] = None,
        on_request: Optional[Callable[[Request], None]] = None,
        on_response: Optional[Callable[[Response], None]] = None,
    ) -> None:
        self._headless = headless
        self._executable_path = executable_path
        self._user_data_dir = user_data_dir
        self._on_request = on_request
        self._on_response = on_response

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Startup / teardown
    # ------------------------------------------------------------------

    async def start(self) -> "BrowserManager":
        """Start Playwright, launch the browser, and create a context."""
        logger.info(
            "Starting Playwright browser (headless=%s) …", self._headless
        )
        self._playwright = await async_playwright().start()

        launch_kwargs: dict = {
            "headless": self._headless,
            "args": BROWSER_LAUNCH_ARGS,
        }
        if self._executable_path:
            launch_kwargs["executable_path"] = self._executable_path

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        logger.debug("Browser launched — version: %s", self._browser.version)

        self._context = await self._create_context()
        logger.info("Browser context created.")
        return self

    async def stop(self) -> None:
        """Gracefully close browser and Playwright."""
        try:
            if self._context:
                await self._context.close()
        except Exception as exc:
            logger.debug("Error closing context: %s", exc)
        try:
            if self._browser:
                await self._browser.close()
        except Exception as exc:
            logger.debug("Error closing browser: %s", exc)
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.debug("Error stopping playwright: %s", exc)

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("Browser shut down.")

    async def _create_context(self) -> BrowserContext:
        """Build a BrowserContext with realistic fingerprinting."""
        assert self._browser is not None

        context_kwargs: dict = {
            "viewport": VIEWPORT,
            "user_agent": USER_AGENT,
            "locale": LOCALE,
            "timezone_id": TIMEZONE,
            "color_scheme": "light",
            "extra_http_headers": EXTRA_HTTP_HEADERS,
            "java_script_enabled": True,
            "accept_downloads": False,
            "ignore_https_errors": False,
            "geolocation": self._random_ru_geolocation(),
            "permissions": ["geolocation"],
        }

        context = await self._browser.new_context(**context_kwargs)

        # Inject stealth script into every new document
        await context.add_init_script(script=_STEALTH_INIT_SCRIPT)

        context.set_default_timeout(PAGE_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

        # Network interception
        if self._on_request or self._on_response:
            await context.route("**/*", self._route_handler)

        if self._on_request:
            context.on("request", self._on_request)
        if self._on_response:
            context.on("response", self._on_response)

        return context

    @staticmethod
    def _random_ru_geolocation() -> dict:
        """Return a random geo-coordinate somewhere in European Russia."""
        return {
            "latitude": round(random.uniform(55.5, 56.5), 4),
            "longitude": round(random.uniform(36.8, 38.5), 4),
            "accuracy": round(random.uniform(20.0, 80.0), 1),
        }

    async def _route_handler(self, route, request: Request) -> None:
        """Intercept network requests — log watched patterns, pass everything through."""
        url = request.url
        if any(
            pattern.replace("**/", "").replace("*", "") in url
            for pattern in NETWORK_WATCH_PATTERNS
        ):
            logger.debug("NET [%s] %s", request.method, url)
        await route.continue_()

    # ------------------------------------------------------------------
    # Page factory
    # ------------------------------------------------------------------

    async def new_page(self) -> Page:
        """Open a new page in the current context and apply default settings."""
        assert self._context is not None, "Call start() before new_page()"

        page = await self._context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

        # Dismiss unexpected dialogs automatically
        page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
        # Log console errors from the page
        page.on("console", self._log_page_console)

        self._page = page
        logger.debug("New page opened.")
        return page

    @property
    def page(self) -> Optional[Page]:
        return self._page

    @property
    def context(self) -> Optional[BrowserContext]:
        return self._context

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _log_page_console(msg) -> None:
        if msg.type in ("error", "warning"):
            logger.debug("PAGE CONSOLE [%s]: %s", msg.type.upper(), msg.text)

    async def screenshot(self, name: str = "screenshot") -> Path:
        """Save a PNG screenshot; returns the file path."""
        if self._page is None:
            raise RuntimeError("No active page to screenshot.")
        from datetime import datetime
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOTS_DIR / f"{name}_{ts}.png"
        await self._page.screenshot(path=str(path), full_page=False)
        logger.info("Screenshot saved: %s", path)
        return path

    async def safe_screenshot(self, name: str = "error") -> None:
        """Take a screenshot and swallow errors (for use in exception handlers)."""
        if not SCREENSHOT_ON_ERROR:
            return
        try:
            await self.screenshot(name)
        except Exception as exc:
            logger.debug("Screenshot failed: %s", exc)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BrowserManager":
        return await self.start()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()
