"""Ядро: браузер и имитация человеческого поведения."""

from __future__ import annotations

from .anti_bot import HumanBehavior
from .browser import BrowserManager

__all__ = ["BrowserManager", "HumanBehavior"]
