"""
Проверка ключевых данных (Starvell session) перед сохранением.
"""

from __future__ import annotations

from starvell_api import StarvellAPI
from utils.starvell_format import format_rub_balance


async def test_starvell_session(session_cookie: str) -> tuple[bool, str, dict]:
    """
    Проверяет cookie session на Starvell.
    Возвращает (успех, сообщение, данные пользователя).
    """
    cookie = (session_cookie or "").strip()
    if len(cookie) < 10:
        return False, "Cookie слишком короткий", {}

    api = StarvellAPI(session_cookie=cookie, delay_seconds=0.5)
    try:
        info = await api.fetch_homepage()
    except Exception as exc:
        return False, f"Ошибка подключения: {exc}", {}

    if not info.get("authorized"):
        return False, "Сессия недействительна или истекла", {}

    user = info.get("user") or {}
    username = user.get("username") or user.get("id") or "?"
    balance = format_rub_balance(user.get("balance"))
    return True, f"Авторизован: {username} | Баланс: {balance}", info
