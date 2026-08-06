"""
Небольшой smoke-тест логики триала без VK API.
Запуск из папки vk_vpn_bot:
    python tests/test_trial.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Корень проекта в PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_settings
from database import activate_trial, close_db, get_or_create_user, get_user, init_db
from services.vpn_keys import generate_vpn_key


async def main() -> None:
    await init_db()
    settings = get_settings()

    uid = 424242
    user = await get_or_create_user(uid, first_name="Demo")
    # Сбрасываем состояние на случай повторного прогона
    if user.is_trial_used:
        from database.db import get_session
        from sqlalchemy import select
        from database.models import User as UserModel

        async with get_session() as session:
            result = await session.execute(select(UserModel).where(UserModel.user_id == uid))
            u = result.scalar_one()
            u.is_trial_used = False
            u.subscription_end = None
            u.vpn_key = None
            await session.commit()
        user = await get_user(uid)
        assert user is not None

    assert user.is_trial_used is False
    assert user.is_subscription_active() is False

    key = generate_vpn_key(user.user_id, settings)
    assert str(uid) in key or key.startswith("vless://") or key.startswith("ss://") or "[Interface]" in key

    user = await activate_trial(user.user_id, key, settings.trial_days)
    assert user.is_trial_used is True
    assert user.is_subscription_active() is True
    assert user.vpn_key == key

    again = await get_user(uid)
    assert again is not None and again.is_trial_used is True

    await close_db()
    print("test_trial: OK")


if __name__ == "__main__":
    asyncio.run(main())
