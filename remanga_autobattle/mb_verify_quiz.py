"""Verify MangaBuff quiz auto-farm: correct_text → streak."""
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
    format="%(levelname)s %(name)s: %(message)s",
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
    await svc.start(headless=True)
    await svc.ensure_login()
    stats = await svc.run_quiz_farm(max_answers=25)
    print(
        json.dumps(
            {
                "stats": stats,
                "quiz_correct_total": svc.stats.quiz_correct,
                "quiz_best": svc.stats.quiz_best_streak,
                "quiz_last": svc.stats.quiz_last_streak,
                "cache_size": len(svc._quiz_cache),
            },
            ensure_ascii=False,
        )
    )
    assert int(stats.get("correct") or 0) >= 10, stats
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
