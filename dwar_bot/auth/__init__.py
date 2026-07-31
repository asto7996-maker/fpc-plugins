"""Подпакет авторизации и управления cookie-сессиями."""

from __future__ import annotations

from .cookie_manager import (
    Cookie,
    CookieManager,
    CookieValidationError,
    SessionProfile,
)

__all__ = [
    "Cookie",
    "CookieManager",
    "CookieValidationError",
    "SessionProfile",
]
