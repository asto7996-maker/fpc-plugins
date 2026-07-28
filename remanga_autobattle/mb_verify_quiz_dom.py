"""Validate DOM quiz path can pass the old ~10-26 fail wall, then lock."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from config import load_config
from services.mangabuff_service import MangaBuffService


async def main() -> None:
    cfg = load_config()
    svc = MangaBuffService(
        cfg,
        user_data_dir=cfg.mangabuff_user_data_dir,
        delay_min_sec=0.02,
        delay_max_sec=0.05,
        email=cfg.mangabuff_email,
        password=cfg.mangabuff_password,
    )
    svc.stats.quiz_target_locked = False
    await svc.start(headless=True)
    await svc.ensure_login()
    # 35 + intentional wrong — must clear old anti-desync wall
    stats = await svc._run_quiz_farm_unlocked(max_answers=80, target_streak=35)
    print("RESULT", json.dumps(stats, ensure_ascii=False), flush=True)
    assert int(stats.get("correct") or 0) >= 35, stats
    assert int(stats.get("locked") or 0) == 1, stats
    # prepare for production 555
    svc.stats.quiz_target_locked = False
    svc._persist_stats()
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
