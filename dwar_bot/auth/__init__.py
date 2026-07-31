"""Модули авторизации: работа с куками и ротация сессий."""

from __future__ import annotations

from .cookie_manager import (
    Cookie,
    CookieFormatError,
    CookieManager,
    CookieValidationError,
    SessionProfile,
)

__all__ = [
    "Cookie",
    "CookieFormatError",
    "CookieManager",
    "CookieValidationError",
    "SessionProfile",
]
