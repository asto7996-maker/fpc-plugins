"""
Администрирование VK-бота и кабинета.

Главный админ получает все тикеты поддержки и видит в боте раздел
управления мини-приложением (автовход в /admin кабинета Bedolaga).
"""

from __future__ import annotations

import logging

from config import Settings
from services.bedolaga import VK_TELEGRAM_ID_OFFSET

logger = logging.getLogger(__name__)

# @xylophaze — подтверждён через VK API users.get
DEFAULT_MAIN_ADMIN_USERNAME = "xylophaze"
DEFAULT_MAIN_ADMIN_VK_ID = 634094665


def main_admin_vk_id(settings: Settings) -> int | None:
    """VK ID главного администратора или None, если не задан."""
    if settings.main_admin_vk_id > 0:
        return settings.main_admin_vk_id
    return None


def is_main_admin(vk_user_id: int, settings: Settings) -> bool:
    """Проверяет, что пользователь — главный админ бота."""
    admin_id = main_admin_vk_id(settings)
    return admin_id is not None and int(vk_user_id) == admin_id


def vk_pseudo_telegram_id(vk_user_id: int) -> int:
    """
    Псевдо-telegram_id Bedolaga для VK-пользователя.

    Именно его нужно добавить в ADMIN_IDS на сервере кабинета, чтобы
    главный админ мог открывать /admin после auto-login из VK-бота.
    """
    return VK_TELEGRAM_ID_OFFSET + int(vk_user_id)


def bedolaga_admin_id_for_main_admin(settings: Settings) -> int | None:
    """ADMIN_IDS-значение для главного админа в .env Bedolaga."""
    admin_id = main_admin_vk_id(settings)
    if admin_id is None:
        return None
    return vk_pseudo_telegram_id(admin_id)


async def resolve_ticket_recipients(api, settings: Settings) -> list[int]:
    """
    Кому пересылать тикеты поддержки.

    Все обращения идут только главному админу. Если он не задан в .env,
    используется SUPPORT_ADMIN_IDS, затем — управляющие сообщества VK.
    """
    admin_id = main_admin_vk_id(settings)
    if admin_id is not None:
        return [admin_id]

    if settings.support_admin_ids:
        return list(settings.support_admin_ids)

    # Fallback: управляющие сообщества (как раньше)
    from services.support import resolve_admin_ids

    return await resolve_admin_ids(api, settings)
