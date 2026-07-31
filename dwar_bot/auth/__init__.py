"""Модуль авторизации и управления сессиями."""

from .cookie_manager import (
    CookieManager,
    CookieValidationError,
    CookieLoadError,
    SessionProfile,
)

__all__ = [
    "CookieManager",
    "CookieValidationError",
    "CookieLoadError",
    "SessionProfile",
]
