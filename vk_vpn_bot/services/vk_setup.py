"""
Автонастройка Long Poll API сообщества VK при старте бота.
"""

from __future__ import annotations

import logging

from vkbottle import API

logger = logging.getLogger(__name__)


async def ensure_long_poll(api: API, group_id: int) -> None:
    """
    Включает Bots Long Poll и нужные события:
    message_new, message_event (callback-кнопки), message_allow/deny.
    """
    if not group_id:
        # Пытаемся определить ID группы по токену
        try:
            groups = await api.groups.get_by_id()
            if groups and getattr(groups, "groups", None):
                group_id = int(groups.groups[0].id)
            elif isinstance(groups, list) and groups:
                group_id = int(groups[0].id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось определить GROUP_ID: %s", exc)
            return

    try:
        await api.groups.set_long_poll_settings(
            group_id=group_id,
            enabled=1,
            api_version="5.199",
            message_new=1,
            message_event=1,
            message_allow=1,
            message_deny=1,
        )
        settings = await api.groups.get_long_poll_settings(group_id=group_id)
        logger.info(
            "Long Poll включён для group_id=%s (enabled=%s)",
            group_id,
            getattr(settings, "is_enabled", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось настроить Long Poll: %s", exc)
