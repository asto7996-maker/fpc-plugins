#!/usr/bin/env python3
"""One-shot publish test via saved userbot session."""
import asyncio
from pathlib import Path

from database import Database
from poster import ChannelPoster
from userbot_auth import UserbotAuth
import config


async def main() -> None:
    db = Database(config.DATABASE_PATH)
    s = db.get_settings()
    src = (s.source_channel or "").strip()
    dst = (s.target_channel or "").strip()
    if src and not src.startswith("@") and not src.lstrip("-").isdigit():
        src = "@" + src
        db.set_source_channel(src)
    if dst and not dst.startswith("@") and not dst.lstrip("-").isdigit():
        dst = "@" + dst
        db.set_target_channel(dst)
    print("settings", src, "->", dst, "progress", db.get_progress_id())

    auth = UserbotAuth(db=db, workdir=Path(__file__).resolve().parent)
    ok = await auth.try_start_existing()
    print("userbot_ok", ok)
    if not ok or auth.client is None:
        return

    me = await auth.client.get_me()
    print("me", me.id, me.username or me.first_name)
    schat = await auth.client.get_chat(src)
    tchat = await auth.client.get_chat(dst)
    print("source", schat.title, schat.id)
    print("target", tchat.title, tchat.id)

    pid = db.get_progress_id()
    for mid in range(pid + 1, pid + 30):
        msg = await auth.client.get_messages(src, mid)
        empty = msg is None or getattr(msg, "empty", False)
        if empty:
            continue
        print(
            "found",
            mid,
            "grouped",
            getattr(msg, "media_group_id", None),
            "photo",
            bool(getattr(msg, "photo", None)),
            "video",
            bool(getattr(msg, "video", None)),
        )
        break
    else:
        print("NO_CONTENT_IN_NEXT_30")

    poster = ChannelPoster(auth.client, db)
    db.set_running(True)
    old = db.get_settings().posts_per_cycle
    db.set_posts_per_cycle(1)
    try:
        n = await poster.run_cycle()
        print("PUBLISHED", n, "progress", db.get_progress_id())
    finally:
        db.set_posts_per_cycle(old)
        db.set_running(False)
        await auth.stop()


if __name__ == "__main__":
    asyncio.run(main())
