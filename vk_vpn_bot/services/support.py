"""
Поддержка внутри VK-бота.

Диалог с сообществом — это и есть канал поддержки: пользователь уже пишет
в тот же чат, который читают администраторы. Поэтому кнопка не должна
никуда «уводить»: вопрос принимается здесь, а администраторам уходит
уведомление, чтобы обращение не потерялось в общем потоке.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

from config import Settings

logger = logging.getLogger(__name__)

# Администраторы сообщества, найденные через API (кэш до перезапуска).
_admin_cache: list[int] | None = None


def is_self_dialog_url(url: str, group_id: int) -> bool:
    """
    Ведёт ли ссылка в диалог с этим же сообществом.

    Такая кнопка бесполезна: пользователь уже находится в этом чате,
    нажатие вернёт его туда же.
    """
    url = (url or "").strip()
    if not url or not group_id:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.netloc.endswith("vk.com"):
        return False
    if parsed.path.rstrip("/") not in {"/im", "/im.php"}:
        return False
    sel = (parse_qs(parsed.query or "").get("sel") or [""])[0]
    if not sel.lstrip("-").isdigit():
        return False
    return abs(int(sel)) == abs(int(group_id))


def usable_support_url(settings: Settings) -> str:
    """Ссылка поддержки, если она действительно куда-то ведёт."""
    url = (settings.support_url or "").strip()
    if not url.startswith("https://"):
        return ""
    if is_self_dialog_url(url, settings.group_id):
        return ""
    return url


async def resolve_admin_ids(api, settings: Settings) -> list[int]:
    """
    ID администраторов для уведомлений.

    Приоритет у SUPPORT_ADMIN_IDS из .env; если он пуст — спрашиваем
    у VK управляющих сообщества.
    """
    global _admin_cache

    if settings.support_admin_ids:
        return list(settings.support_admin_ids)

    if _admin_cache is not None:
        return _admin_cache

    admins: list[int] = []
    try:
        response = await api.request(
            "groups.getMembers",
            {"group_id": abs(int(settings.group_id)), "filter": "managers"},
        )
        for item in response.get("response", {}).get("items", []):
            uid = item.get("id")
            if isinstance(uid, int) and uid > 0:
                admins.append(uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("не удалось получить управляющих сообщества: %s", exc)

    _admin_cache = admins
    return admins


async def notify_admins(
    api,
    settings: Settings,
    *,
    vk_user_id: int,
    first_name: str | None,
    question: str,
) -> int:
    """
    Пересылает вопрос администраторам. Возвращает число доставленных копий.

    Ноль — не потеря обращения: сообщение пользователя всё равно осталось
    в диалоге сообщества, просто уведомление не ушло.
    """
    admins = await resolve_admin_ids(api, settings)
    if not admins:
        logger.warning("нет администраторов для уведомления о вопросе")
        return 0

    who = (first_name or "пользователь").strip()
    text = (
        f"🆘 Новый вопрос в поддержку\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"От: {who} (vk.com/id{vk_user_id})\n\n"
        f"{question.strip()[:3000]}\n\n"
        f"Ответить: https://vk.com/gim{abs(int(settings.group_id))}?sel={vk_user_id}"
    )

    delivered = 0
    for admin_id in admins:
        try:
            await api.request(
                "messages.send",
                {"user_id": admin_id, "message": text, "random_id": 0},
            )
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("не доставлено админу %s: %s", admin_id, exc)
    return delivered
