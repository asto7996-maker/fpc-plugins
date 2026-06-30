#!/usr/bin/env python3
"""Проверка BOT_TOKEN без запуска всего бота."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    from config import load_settings

    s = load_settings()
    token = (s.bot_token or "").strip()
    if not token:
        print("FAIL: bot_token пустой в config/settings.json")
        return 1
    if ":" not in token or len(token) < 20:
        print(f"FAIL: bot_token выглядит некорректно (длина {len(token)})")
        return 1

    from aiogram import Bot
    from aiogram.exceptions import TelegramUnauthorizedError

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        wh = await bot.get_webhook_info()
        print(f"OK: @{me.username} (id={me.id})")
        if wh.url:
            print(f"WARN: активен webhook {wh.url!r} — polling не работал бы (удалится при старте)")
        else:
            print("OK: webhook не задан")
        return 0
    except TelegramUnauthorizedError:
        print("FAIL: Telegram отклонил токен (Unauthorized)")
        return 2
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 3
    finally:
        await bot.session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
