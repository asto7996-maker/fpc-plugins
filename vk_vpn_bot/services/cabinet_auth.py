"""
Автологин в кабинет Paskod для пользователей VK.

Telegram-пользователи входят через WebApp initData без регистрации.
Для VK используем тот же механизм, что у guest-purchase:
  JWT type=auto_login → https://cabinet.paskod.ru/auto-login?token=...
  Cabinet вызывает POST /cabinet/auth/login/auto и выдаёт сессию.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

import jwt

from config import Settings

logger = logging.getLogger(__name__)

JWT_ALGORITHM = "HS256"
DEFAULT_TTL_HOURS = 72


def create_auto_login_token(
    bedolaga_user_id: int,
    cabinet_jwt_secret: str,
    *,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> str:
    """
    Создаёт short-lived JWT как create_auto_login_token() в Bedolaga.

    iat ставится на 2 минуты в прошлое, чтобы пережить рассинхрон часов
    между сервером VK-бота и сервером кабинета.
    """
    now = int(time.time()) - 120
    payload = {
        "sub": str(int(bedolaga_user_id)),
        "type": "auto_login",
        "exp": now + int(ttl_hours) * 3600,
        "iat": now,
    }
    return jwt.encode(payload, cabinet_jwt_secret, algorithm=JWT_ALGORITHM)


def _safe_redirect(redirect: str | None) -> str:
    """Только относительные пути внутри кабинета (anti open-redirect)."""
    if not redirect:
        return "/"
    path = redirect.strip() or "/"
    if not path.startswith("/") or path.startswith("//"):
        return "/"
    if any(ch in path for ch in ("\\", "\n", "\r")):
        return "/"
    return path


def build_auto_login_url(
    bedolaga_user_id: int,
    settings: Settings,
    *,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    redirect: str | None = "/",
) -> str:
    """
    Полная ссылка входа в кабинет без регистрации/пароля.

    После успешного auto-login SPA уходит на `redirect`
    (например /subscription/purchase).
    """
    if not settings.cabinet_jwt_secret:
        raise RuntimeError("CABINET_JWT_SECRET не задан")
    token = create_auto_login_token(
        bedolaga_user_id,
        settings.cabinet_jwt_secret,
        ttl_hours=ttl_hours,
    )
    base = settings.cabinet_url.rstrip("/")
    target = _safe_redirect(redirect)
    return (
        f"{base}/auto-login"
        f"?token={quote(token, safe='')}"
        f"&redirect={quote(target, safe='')}"
    )


async def ensure_panel_user(
    vk_user_id: int,
    first_name: str | None,
) -> int:
    """Тихая авторегистрация VK-пользователя в панели Bedolaga."""
    from database import get_or_create_user, set_bedolaga_user_id
    from services.bedolaga import get_bedolaga_client

    client = get_bedolaga_client()
    if not client or not client.enabled:
        raise RuntimeError("Bedolaga API недоступен — нужен BEDOLAGA_API_KEY")

    await get_or_create_user(vk_user_id, first_name=first_name)
    panel_user = await client.ensure_user(vk_user_id, first_name=first_name)
    bedolaga_id = int(panel_user.get("id") or 0)
    if not bedolaga_id:
        raise RuntimeError(f"Не удалось создать пользователя панели: {panel_user}")
    await set_bedolaga_user_id(vk_user_id, bedolaga_id)
    return bedolaga_id


async def ensure_auto_login_url(
    vk_user_id: int,
    first_name: str | None,
    settings: Settings,
    *,
    redirect: str | None = "/",
) -> tuple[str, int]:
    """
    Гарантирует пользователя в Bedolaga и возвращает (url, bedolaga_user_id).
    Регистрация происходит автоматически — без email/пароля.
    """
    bedolaga_id = await ensure_panel_user(vk_user_id, first_name)
    url = build_auto_login_url(bedolaga_id, settings, redirect=redirect)
    logger.info(
        "Auto-login URL for vk=%s bedolaga=%s redirect=%s",
        vk_user_id,
        bedolaga_id,
        _safe_redirect(redirect),
    )
    return url, bedolaga_id
