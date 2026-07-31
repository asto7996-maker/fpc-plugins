"""Verify turbo read speed + battles + trades on VPS."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
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
    svc.steps_min = 1
    svc.steps_max = 1
    await svc.start(headless=True)
    assert svc._page is not None
    page = svc._page
    await svc.ensure_login()

    print("=== BATTLES ===")
    wins = await svc._run_card_battles(page, max_fights=2)
    aw = await svc._run_card_awakening(page, max_cards=1)
    print(f"wins={wins} awakening={aw} total_won={svc.stats.battles_won} total={svc.stats.battles_total}")

    print("=== TRADES ===")
    sent = await svc._run_card_trades_unlocked(offers=3)
    up = await svc._run_card_upgrades_unlocked(max_ops=1)
    print(f"trades_sent={sent} upgrades={up} total_trades={svc.stats.trades_sent}")

    print("=== TURBO READ 8 chapters ===")
    titles = await svc.fetch_popular_titles(limit=3)
    title = titles[0] if titles else None
    if not title:
        print("no titles")
        await svc.stop()
        return
    slug = str(title.get("slug") or "")
    start = await svc._open_first_chapter_with_retry(title.get("href") or "", slug)
    print("start", start)
    t0 = time.time()
    confirmed = 0
    for i in range(8):
        url = page.url.split("?")[0]
        if not re.search(r"/manga/[^/]+/\d+/\d+", url):
            break
        if await svc._chapter_already_read_on_site(page):
            if not await svc._go_next_chapter_with_retry(page):
                break
            continue
        await svc._smooth_read_chapter(page)
        await svc._wait_for_chapter_context(page)
        c = await svc._register_chapter_read(page, force_flush=False)
        pending = await svc._history_pool_size(page)
        print(f"  ch{i+1} c={c} pending={pending} url={page.url.split('?')[0]}")
        if pending >= svc._turbo_pool_flush_at():
            await svc._go_next_chapter_with_retry(page)
            c2 = await svc._flush_history_pool_confirmed(page)
            confirmed += c2
            print(f"    flush +{c2}")
        else:
            confirmed += c
            if not await svc._go_next_chapter_with_retry(page):
                break
            gap = svc._history_min_gap_for_tier()
            ready = (time.time() - svc._last_history_post_at) >= gap
            if pending >= 2 and ready:
                c2 = await svc._flush_history_pool_confirmed(page)
                confirmed += c2
                print(f"    gap-flush +{c2}")
    tail = await svc._register_chapter_read(page, force_flush=True)
    confirmed += tail
    elapsed = time.time() - t0
    print(
        json.dumps(
            {
                "confirmed": confirmed,
                "elapsed_sec": round(elapsed, 1),
                "sec_per_chapter": round(elapsed / max(1, confirmed), 2),
                "battles_won": svc.stats.battles_won,
                "trades_sent": svc.stats.trades_sent,
                "cards": svc.stats.cards_claimed,
            },
            ensure_ascii=False,
        )
    )
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
