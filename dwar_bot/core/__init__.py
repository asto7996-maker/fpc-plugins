"""Ядро браузерного движка и anti-bot утилит."""

from dwar_bot.core.browser import BrowserEngine, BrowserEngineError, NavigationError

__all__ = [
    "BrowserEngine",
    "BrowserEngineError",
    "NavigationError",
]
