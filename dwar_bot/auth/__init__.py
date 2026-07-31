"""Подпакет авторизации: управление куки-сессиями."""

from __future__ import annotations

from .cookie_manager import (
    CookieError,
    CookieManager,
    CookieValidationError,
    NoSessionsAvailableError,
    Session,
    build_default_manager,
)

__all__ = [
    "CookieManager",
    "Session",
    "CookieError",
    "CookieValidationError",
    "NoSessionsAvailableError",
    "build_default_manager",
]
