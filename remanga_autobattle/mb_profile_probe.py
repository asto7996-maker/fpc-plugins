"""Probe MangaBuff profile reading stats before/after chapter read."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

DEFAULT_USER = Path(__file__).resolve().parent / "user_data_mangabuff"


async def profile_snapshot(page) -> dict:
    """Extract reading-related stats from profile and daily pages."""
    out: dict = {}
    user_id = ""
    try:
        await page.goto("https://mangabuff.ru/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)
        user_id = str(
            await page.evaluate(
                "() => (window.user_id != null ? String(window.user_id) : '')"
            )
            or ""
        ).strip()
        out["user_id"] = user_id
    except Exception as exc:
        out["user_id_error"] = str(exc)

    urls = [("https://mangabuff.ru/", "home")]
    if user_id.isdigit():
        urls.extend(
            [
                (f"https://mangabuff.ru/users/{user_id}", "profile"),
                (f"https://mangabuff.ru/users/{user_id}/history", "history"),
            ]
        )

    for url, key in urls:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
            data = await page.evaluate(
                """() => {
                  const text = (document.body && document.body.innerText) || '';
                  const nums = [...text.matchAll(/(\\d+)/g)].map(m => m[1]);
                  const chapterMentions = text.match(/(\\d+)\\s*глав[аыи]?/gi) || [];
                  const readMentions = text.match(/прочитан[оа]?\\s*[:\\-]?\\s*(\\d+)/gi) || [];
                  const questMentions = text.match(/задани[ея][^\\d]*(\\d+)/gi) || [];
                  const dailyMentions = text.match(/сегодня[^\\d]*(\\d+)/gi) || [];
                  const links = [...document.querySelectorAll('a[href*="/manga/"]')]
                    .slice(0, 5)
                    .map(a => ({t: (a.innerText||'').trim().slice(0,40), h: a.href.split('?')[0]}));
                  return {
                    url: location.href,
                    title: document.title.slice(0, 80),
                    textSample: text.slice(0, 2500),
                    chapterMentions: chapterMentions.slice(0, 8),
                    readMentions: readMentions.slice(0, 8),
                    questMentions: questMentions.slice(0, 8),
                    dailyMentions: dailyMentions.slice(0, 8),
                    nums: nums.slice(0, 30),
                    links,
                  };
                }"""
            )
            out[key] = data
        except Exception as exc:
            out[key] = {"error": str(exc)}
    return out


def _diff_profiles(before: dict, after: dict) -> dict:
    """Compare text samples and chapter mentions between snapshots."""
    changes: dict = {}
    skip_keys = {"user_id"}
    for key in set(before) | set(after):
        if key in skip_keys:
            continue
        b = before.get(key)
        a = after.get(key)
        if not isinstance(b, dict) or not isinstance(a, dict):
            if b != a:
                changes[key] = {"before": b, "after": a}
            continue
        if b.get("textSample") != a.get("textSample"):
            changes[key] = {
                "chapterMentions_before": b.get("chapterMentions"),
                "chapterMentions_after": a.get("chapterMentions"),
                "readMentions_before": b.get("readMentions"),
                "readMentions_after": a.get("readMentions"),
                "text_before_tail": str(b.get("textSample") or "")[-400:],
                "text_after_tail": str(a.get("textSample") or "")[-400:],
            }
    return changes


async def read_one_chapter(page, url: str, gap_sec: float = 8.0) -> dict:
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    before = await page.evaluate(
        """() => ({
          cc: window.current_chapter,
          is_read: typeof is_read !== 'undefined' ? !!is_read : null,
          read_status_send: typeof read_status_send !== 'undefined' ? !!read_status_send : null,
        })"""
    )
    if before.get("read_status_send"):
        return {"url": url, "skipped": "already_sent", "before": before}

    for _ in range(30):
        st = await page.evaluate(
            "() => ({is_read: typeof is_read !== 'undefined' ? !!is_read : false, y: scrollY, h: document.body.scrollHeight, vh: innerHeight})"
        )
        if st["is_read"]:
            break
        y = min(int(st["y"] + st["vh"] * 0.6), int(st["h"] - st["vh"]))
        await page.evaluate(
            "(ty)=>{window.scrollTo(0,ty);window.dispatchEvent(new Event('scroll'));if(window.jQuery)window.jQuery(window).trigger('scroll');}",
            y,
        )
        await asyncio.sleep(0.08)

    await page.evaluate(
        "() => { const h=document.body.scrollHeight; window.scrollTo(0,h); if(window.jQuery)window.jQuery(window).trigger('scroll'); }"
    )
    await asyncio.sleep(0.3)
    if await page.evaluate("typeof addHistory === 'function'"):
        await page.evaluate("() => addHistory()")
    pool = await page.evaluate(
        "() => JSON.parse(localStorage.getItem('history_pool') || '[]')"
    )
    post = None
    if pool:
        await asyncio.sleep(gap_sec)
        post = await page.evaluate(
            """async () => {
              const items = JSON.parse(localStorage.getItem('history_pool') || '[]');
              const body = new URLSearchParams();
              items.forEach((it,i)=>{body.append('items['+i+'][manga_id]',String(it.manga_id));body.append('items['+i+'][chapter_id]',String(it.chapter_id));});
              const csrfEl = document.querySelector('meta[name=csrf-token]');
              const csrf = csrfEl ? csrfEl.getAttribute('content') : '';
              const resp = await fetch('/addHistory?r=702',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','X-CSRF-TOKEN':csrf,'X-Requested-With':'XMLHttpRequest'},body:body.toString(),credentials:'same-origin'});
              const raw = await resp.text();
              let json = null;
              try { json = JSON.parse(raw); } catch (e) { json = {raw: raw.slice(0, 500)}; }
              if (resp.status>=200 && resp.status<300) localStorage.setItem('history_pool','[]');
              return {status: resp.status, json, count: items.length};
            }"""
        )
    after = await page.evaluate(
        """() => ({
          is_read: typeof is_read !== 'undefined' ? !!is_read : null,
          cc: window.current_chapter,
        })"""
    )
    return {"url": url, "before": before, "pool": pool, "post": post, "after": after}


async def main() -> None:
    parser = argparse.ArgumentParser(description="MangaBuff profile read probe")
    parser.add_argument("--user-data", default=str(DEFAULT_USER))
    parser.add_argument("--slug", default="kak-zashchitit-starshego-brata-glavnoi-geroini")
    parser.add_argument("--chapters", type=int, default=2, help="Chapters to read from Continue")
    parser.add_argument("--gap", type=float, default=8.0, help="addHistory gap seconds")
    args = parser.parse_args()

    user_dir = Path(args.user_data)
    if not user_dir.exists():
        print(f"ERROR: user data dir not found: {user_dir}", file=sys.stderr)
        sys.exit(1)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_dir),
            headless=True,
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print("=== BEFORE PROFILE ===")
        before_prof = await profile_snapshot(page)
        print(json.dumps(before_prof, ensure_ascii=False, indent=2)[:6000])

        slug = args.slug
        await page.goto(f"https://mangabuff.ru/manga/{slug}", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        title_info = await page.evaluate(
            """() => {
              const cont = [...document.querySelectorAll('a')].find(a => /продолж/i.test((a.innerText||'').trim()));
              const readBtn = [...document.querySelectorAll('a')].find(a => /^читать$/i.test((a.innerText||'').trim()));
              return {
                continueHref: cont ? cont.href.split('?')[0] : null,
                readHref: readBtn ? readBtn.href.split('?')[0] : null,
                textSample: (document.body.innerText || '').slice(0, 800),
              };
            }"""
        )
        print("TITLE", json.dumps(title_info, ensure_ascii=False))

        start_url = title_info.get("continueHref") or title_info.get("readHref")
        if not start_url:
            start_url = f"https://mangabuff.ru/manga/{slug}/1/1"
        print("START", start_url)

        reads = []
        url = start_url
        for i in range(args.chapters):
            r = await read_one_chapter(page, url, gap_sec=args.gap)
            reads.append(r)
            print(f"READ{i+1}", json.dumps(r, ensure_ascii=False))
            nxt = await page.evaluate(
                """() => {
                  for (const el of document.querySelectorAll('a[href*="/manga/"]')) {
                    if (/след\\.?\\s*глава/i.test((el.innerText||'').trim())) {
                      return el.href.split('?')[0];
                    }
                  }
                  return '';
                }"""
            )
            if not nxt:
                break
            url = nxt
            await asyncio.sleep(1.0)

        await asyncio.sleep(3)
        print("=== AFTER PROFILE ===")
        after_prof = await profile_snapshot(page)
        print(json.dumps(after_prof, ensure_ascii=False, indent=2)[:6000])

        changes = _diff_profiles(before_prof, after_prof)
        print("=== PROFILE CHANGES ===")
        print(json.dumps(changes, ensure_ascii=False, indent=2))

        ok_reads = sum(
            1 for r in reads
            if r.get("post") and int((r.get("post") or {}).get("status") or 0) == 200
        )
        print(
            f"SUMMARY: chapters_attempted={len(reads)} addHistory_ok={ok_reads} "
            f"profile_changed={bool(changes)}"
        )

        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
