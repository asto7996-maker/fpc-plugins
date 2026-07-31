"""Verify quiz lock-at-target: perfect streak then intentional wrong."""
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
from services.mangabuff_service import MangaBuffService, QUIZ_TARGET_STREAK


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
    # unit: wrong picker
    q = {
        "id": 1,
        "question": "test",
        "answers": ["A", "B", "C", "D"],
        "correct_text": "B",
    }
    wrong = svc._pick_wrong_quiz_answer(q)
    assert wrong in ("A", "C", "D"), wrong
    assert wrong != "B"
    print("WRONG_PICKER_OK", wrong)

    await svc.start(headless=True)
    await svc.ensure_login()
    # сброс локального лока для теста маленькой цели
    svc.stats.quiz_target_locked = False
    # не трогаем реальный best если он уже большой — для теста цель 3
    stats = await svc.run_quiz_farm(target_streak=3)
    print(
        json.dumps(
            {
                "stats": stats,
                "best": svc.stats.quiz_best_streak,
                "last": svc.stats.quiz_last_streak,
                "locked_flag": svc.stats.quiz_target_locked,
                "prod_target": QUIZ_TARGET_STREAK,
            },
            ensure_ascii=False,
        )
    )
    assert stats.get("locked") == 1, stats
    assert stats.get("correct") >= 3, stats
    # после лока повторный запуск с той же целью должен skip
    again = await svc.run_quiz_farm(target_streak=3)
    assert again.get("skipped") == 1, again
    print("SKIP_AFTER_LOCK_OK")
    # не оставляем тестовый lock на проде — сброс, цель 555 ещё впереди
    svc.stats.quiz_target_locked = False
    svc._persist_stats()
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
