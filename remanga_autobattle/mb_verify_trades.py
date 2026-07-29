"""Verify chat/comment trade farm: 1 per user, all inventory ranks."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    assert svc._page is not None
    await svc.ensure_login()
    page = svc._page

    cards = await svc._tradable_inventory_cards(page)
    ranks = [str(c.get("rank") or "?") for c in cards]
    print(
        json.dumps(
            {
                "tradable": len(cards),
                "ranks_sample": ranks[:25],
                "already_receivers": len(svc._trade_receivers),
                "candidates": len(svc._trade_candidates),
            },
            ensure_ascii=False,
        )
    )

    targets = await svc._pick_trade_targets(page, limit=12)
    print("TARGETS", targets)

    sent = await svc._run_card_trades_unlocked(offers=8)
    print(
        json.dumps(
            {
                "sent": sent,
                "trades_sent_total": svc.stats.trades_sent,
                "receivers_now": len(svc._trade_receivers),
                "last_action": svc.stats.last_action,
            },
            ensure_ascii=False,
        )
    )
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
