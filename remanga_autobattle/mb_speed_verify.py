"""Measure turbo chapter credit rate on VPS (~12 chapters)."""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from config import load_config
from services.mangabuff_service import MangaBuffService
from ui_theme import SPEED_PRESETS


async def main() -> None:
    cfg = load_config()
    preset = SPEED_PRESETS["turbo"]
    svc = MangaBuffService(
        cfg,
        user_data_dir=cfg.mangabuff_user_data_dir,
        delay_min_sec=preset.delay_min,
        delay_max_sec=preset.delay_max,
        email=cfg.mangabuff_email,
        password=cfg.mangabuff_password,
    )
    svc.set_delay(
        preset.delay_min,
        preset.delay_max,
        preset.steps_min,
        preset.steps_max,
    )
    await svc.start(headless=True)
    await svc.ensure_login()
    page = svc._page
    assert page is not None

    titles = await svc.fetch_popular_titles(limit=8)
    start = None
    for title in titles:
        slug = str(title.get("slug") or "")
        start = await svc._open_first_chapter_with_retry(
            title.get("href") or "", slug
        )
        if start:
            break
    if not start:
        print({"error": "no unread title"})
        await svc.stop()
        return

    print("START", start, "tier", svc._tempo_tier())
    before = int(svc.stats.chapters_read or 0)
    t_read = None
    skips = 0
    loops = 0
    target = 12

    while loops < target * 5 and (int(svc.stats.chapters_read or 0) - before) < target:
        if svc._stop_flag.is_set():
            break
        url = page.url.split("?")[0]
        if not re.search(r"/manga/[^/]+/\d+/\d+", url):
            break
        await svc._wait_for_chapter_context(page, timeout_sec=2.5)
        if await svc._chapter_already_read_on_site(page):
            skips += 1
            if not await svc._hop_next_unread_fast(page):
                if not await svc._go_next_chapter_with_retry(page):
                    break
            continue

        if t_read is None:
            t_read = time.time()
            before = int(svc.stats.chapters_read or 0)
            print("UNREAD_AT", page.url, "skips", skips)

        await svc._smooth_read_chapter(page)
        confirmed = await svc._register_chapter_read(page, force_flush=False)
        pending = await svc._history_pool_size(page)
        moved = False
        if pending >= svc._turbo_pool_flush_at():
            moved = await svc._go_next_chapter_with_retry(page)
            confirmed = await svc._flush_history_pool_confirmed(page)
        elif pending >= 1:
            moved = await svc._go_next_chapter_with_retry(page)
            gap_ready = (
                time.time() - svc._last_history_post_at
            ) >= svc._history_min_gap_for_tier()
            if pending >= 2 and gap_ready:
                confirmed = await svc._flush_history_pool_confirmed(page)
        if confirmed > 0:
            svc.stats.chapters_read += confirmed
            for _ in range(confirmed):
                svc.note_chapter_finished()
            print(
                f"OK +{confirmed} total={svc.stats.chapters_read} "
                f"pending={await svc._history_pool_size(page)}"
            )
        elif not moved:
            if not await svc._go_next_chapter_with_retry(page):
                break
        loops += 1

    tail = await svc._register_chapter_read(page, force_flush=True)
    if tail > 0:
        svc.stats.chapters_read += tail
        print(f"TAIL +{tail}")

    elapsed = max(0.1, time.time() - (t_read or time.time()))
    gained = int(svc.stats.chapters_read or 0) - before
    print(
        {
            "gained": gained,
            "elapsed_sec": round(elapsed, 1),
            "sec_per_chapter": round(elapsed / max(1, gained), 2) if gained else None,
            "chapters_per_hour": round(3600 * gained / elapsed, 1) if gained else 0,
            "skips": skips,
            "loops": loops,
            "last_url": page.url,
        }
    )
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
