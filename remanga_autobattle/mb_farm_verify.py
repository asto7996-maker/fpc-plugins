"""Short farm run + profile verification on VPS."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_config  # noqa: E402
from mb_profile_probe import _diff_profiles, profile_snapshot  # noqa: E402
from services.mangabuff_service import MangaBuffService  # noqa: E402


async def read_n_chapters(svc: MangaBuffService, target: int = 5) -> int:
    assert svc._page is not None
    page = svc._page
    titles = await svc.fetch_popular_titles(limit=5)
    if not titles:
        return 0
    title = titles[0]
    slug = str(title.get("slug") or "")
    start = await svc._open_first_chapter_with_retry(
        title.get("href") or "", slug
    )
    if not start:
        print("ERROR: cannot open first chapter", slug)
        return 0
    print("START_URL", start)
    confirmed_total = 0
    for i in range(target):
        if svc._stop_flag.is_set():
            break
        url = page.url.split("?")[0]
        if not re.search(r"/manga/[^/]+/\d+/\d+", url):
            break
        if await svc._chapter_already_read_on_site(page):
            print(f"SKIP re-read {url}")
            if not await svc._go_next_chapter_with_retry(page):
                break
            continue
        print(f"READ {i+1}/{target} {url}")
        await svc._smooth_read_chapter(page)
        if not await svc._wait_for_chapter_context(page):
            print("WARN: no context", url)
            if not await svc._go_next_chapter_with_retry(page):
                break
            continue
        confirmed = await svc._register_chapter_read(page, force_flush=False)
        if confirmed <= 0:
            pending = await svc._history_pool_size(page)
            if pending >= 2:
                confirmed = await svc._flush_history_pool_confirmed(page)
        confirmed_total += max(0, confirmed)
        print(f"  confirmed={confirmed} total={confirmed_total}")
        if i + 1 >= target:
            break
        if not await svc._go_next_chapter_with_retry(page):
            break
    tail = await svc._register_chapter_read(page, force_flush=True)
    confirmed_total += max(0, tail)
    return confirmed_total


async def main() -> None:
    cfg = load_config()
    svc = MangaBuffService(
        cfg,
        user_data_dir=cfg.mangabuff_user_data_dir,
        delay_min_sec=0.03,
        delay_max_sec=0.08,
        email=cfg.mangabuff_email,
        password=cfg.mangabuff_password,
    )
    svc.steps_min = 1
    svc.steps_max = 3

    await svc.start(headless=True)
    assert svc._page is not None
    page = svc._page

    print("=== BEFORE ===")
    before = await profile_snapshot(page)
    print(json.dumps(before, ensure_ascii=False)[:5000])

    if not await svc.ensure_login():
        print("ERROR: not logged in")
        await svc.stop()
        sys.exit(1)

    read = await read_n_chapters(svc, target=5)
    print(f"CHAPTERS_CONFIRMED={read}")

    await asyncio.sleep(4)
    print("=== AFTER ===")
    after = await profile_snapshot(page)
    print(json.dumps(after, ensure_ascii=False)[:5000])
    changes = _diff_profiles(before, after)
    print("=== CHANGES ===")
    print(json.dumps(changes, ensure_ascii=False, indent=2))
    print(
        f"SUMMARY: confirmed={read} profile_changed={bool(changes)} "
        f"errors={svc.stats.errors}"
    )
    await svc.stop()
    if read <= 0:
        sys.exit(2)
    if not changes:
        sys.exit(3)


if __name__ == "__main__":
    asyncio.run(main())
