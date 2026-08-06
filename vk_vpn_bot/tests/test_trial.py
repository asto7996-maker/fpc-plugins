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

    user = await get_or_create_user(42, first_name="Demo")
    assert user.is_trial_used is False
    assert user.is_subscription_active() is False

    key = generate_vpn_key(user.user_id, settings)
    assert "42" in key or key.startswith("vless://") or key.startswith("ss://") or "[Interface]" in key

    user = await activate_trial(user.user_id, key, settings.trial_days)
    assert user.is_trial_used is True
    assert user.is_subscription_active() is True
    assert user.vpn_key == key

    again = await get_user(42)
    assert again is not None and again.is_trial_used is True

    await close_db()
    print("test_trial: OK")


if __name__ == "__main__":
    asyncio.run(main())
