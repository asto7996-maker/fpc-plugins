"""Кто может пользоваться лобби и кому юзербот отвечает."""

from __future__ import annotations

from typing import Optional

import config


def test_usernames() -> set[str]:
    raw = getattr(config, "TEST_USERNAMES", None) or []
    return {str(x).lower().lstrip("@").strip() for x in raw if str(x).strip()}


def is_privileged(user_id: Optional[int], username: str = "") -> bool:
    if user_id and int(user_id) in set(int(x) for x in (config.ADMIN_IDS or [])):
        return True
    uname = (username or "").lower().lstrip("@").strip()
    return bool(uname and uname in test_usernames())


def may_use_lobby(db, user_id: Optional[int], username: str = "") -> bool:
    if is_privileged(user_id, username):
        return True
    if not user_id:
        return False
    row = db.known_get(int(user_id))
    if not row:
        return False
    return int(row.get("blocked") or 0) == 0


def decide_inbox_action(
    *,
    privileged: bool,
    known: bool,
    blocked: bool,
    incoming: bool,
    seed_done: bool,
    unread: bool,
) -> str:
    """
    seed — запомнить непрочитанный чат и позвать в бота.
    remind — уже свой человек, напомнить про бота.
    block — левый пользователь.
    ignore — ничего не писать.
    """
    if blocked and not privileged:
        return "ignore"
    if privileged:
        return "remind" if incoming else "ignore"
    if known:
        return "remind" if incoming else "ignore"
    if not seed_done and unread:
        return "seed"
    if incoming:
        return "block"
    return "ignore"
