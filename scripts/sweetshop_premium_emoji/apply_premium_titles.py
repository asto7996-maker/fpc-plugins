#!/usr/bin/env python3
"""Apply premium <tg-emoji> uniformly to ALL free-message titles (and texts)
in @sweetshopxxx_bot via BOT-T API.

Auth: BOTT_SECRET_KEY or TELEGRAM_BOT_TOKEN (+ optional BOTT_BOT_ID, default 348122).
Packs: NeonEmojis / FireEmojiPack / TranslucentPack / AdaptivePixelEmoji / tgmacicons
(theme: 18+ OnlyFans-style shop — 🔥❤️💋💎✨🔞 etc.).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API = "https://api.bot-t.com"
DEFAULT_BOT_ID = 348122
HERE = Path(__file__).resolve().parent

_TG_BLOCK = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", re.I | re.S)
# Match emoji candidates; prefer longer keys from map when substituting.
_HTML_TAG = re.compile(r"<[^>]+>")


def load_map(path: Path) -> Dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.get("map", data).items() if k and v}


def pe(emoji: str, mapping: Dict[str, str]) -> str:
    eid = mapping.get(emoji) or mapping.get(emoji.replace("\ufe0f", "")) or mapping.get(emoji + "\ufe0f")
    if not eid:
        return emoji
    return f'<tg-emoji emoji-id="{eid}">{html.escape(emoji)}</tg-emoji>'


def inject(text: str, mapping: Dict[str, str]) -> str:
    """Replace bare unicode emojis with premium tags; keep existing tg-emoji blocks."""
    if not text:
        return text
    keys = sorted(mapping.keys(), key=len, reverse=True)
    if not keys:
        return text
    pattern = re.compile("|".join(re.escape(k) for k in keys))

    parts: List[str] = []
    pos = 0
    for block in _TG_BLOCK.finditer(text):
        chunk = text[pos : block.start()]
        parts.append(pattern.sub(lambda m: pe(m.group(0), mapping), chunk))
        parts.append(block.group(0))
        pos = block.end()
    tail = text[pos:]
    parts.append(pattern.sub(lambda m: pe(m.group(0), mapping), tail))
    return "".join(parts)


def api_post(path: str, body: Dict[str, Any], secret: Optional[str], token: Optional[str]) -> Dict[str, Any]:
    q: Dict[str, str] = {}
    if secret:
        q["secretKey"] = secret
    if token:
        q["token"] = token
    url = API + path + (("?" + urllib.parse.urlencode(q)) if q else "")
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {path}: {raw[:500]}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Bad JSON from {path}: {raw[:500]}") from e


def list_messages(bot_id: int, secret: Optional[str], token: Optional[str], limit: int = 100) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        data = api_post(
            "/v1/bot/messagenew/message/index",
            {"bot_id": bot_id, "offset": offset, "limit": limit},
            secret,
            token,
        )
        if not data.get("result"):
            raise RuntimeError(f"list failed: {data}")
        payload = data.get("data") or {}
        items = payload.get("messages") or payload.get("items") or payload.get("list")
        if items is None and isinstance(payload, list):
            items = payload
        if items is None and isinstance(data.get("data"), list):
            items = data["data"]
        if not items:
            # Some BOT-T versions return {count, rows}
            items = payload.get("rows") or []
        if not items:
            break
        out.extend(items)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.15)
    return out


def get_message(bot_id: int, message_id: int, secret: Optional[str], token: Optional[str]) -> Dict[str, Any]:
    data = api_post(
        "/v1/bot/messagenew/message/get",
        {"bot_id": bot_id, "message_id": message_id},
        secret,
        token,
    )
    if not data.get("result"):
        raise RuntimeError(f"get {message_id} failed: {data}")
    return data.get("data") or {}


def update_title(bot_id: int, message_id: int, title: str, secret: Optional[str], token: Optional[str]) -> Any:
    data = api_post(
        "/v1/bot/messagenew/message/update-title",
        {"bot_id": bot_id, "message_id": message_id, "title": title},
        secret,
        token,
    )
    if not data.get("result"):
        raise RuntimeError(f"update-title {message_id} failed: {data}")
    return data.get("data")


def update_text(bot_id: int, message_id: int, text: str, secret: Optional[str], token: Optional[str]) -> Any:
    data = api_post(
        "/v1/bot/messagenew/message/update-text",
        {"bot_id": bot_id, "message_id": message_id, "text": text},
        secret,
        token,
    )
    if not data.get("result"):
        raise RuntimeError(f"update-text {message_id} failed: {data}")
    return data.get("data")


def msg_id_of(row: Dict[str, Any]) -> Optional[int]:
    for k in ("id", "message_id", "messageId"):
        if row.get(k) is not None:
            return int(row[k])
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bot-id", type=int, default=int(os.environ.get("BOTT_BOT_ID") or DEFAULT_BOT_ID))
    ap.add_argument("--map", type=Path, default=HERE / "emoji_map.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--titles-only", action="store_true", default=True)
    ap.add_argument("--also-text", action="store_true", help="Also rewrite message bodies")
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()

    secret = os.environ.get("BOTT_SECRET_KEY") or os.environ.get("SECRET_KEY")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not secret and not token:
        print("Set BOTT_SECRET_KEY (preferred) or TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2

    mapping = load_map(args.map)
    print(f"bot_id={args.bot_id} map_size={len(mapping)} dry_run={args.dry_run}")

    rows = list_messages(args.bot_id, secret, token)
    print(f"messages_listed={len(rows)}")
    if not rows:
        print("No messages returned — check auth / bot_id", file=sys.stderr)
        return 1

    changed_titles = changed_texts = 0
    for row in rows:
        mid = msg_id_of(row)
        if mid is None:
            continue
        full = get_message(args.bot_id, mid, secret, token)
        title = str(full.get("title") or row.get("title") or "")
        text = str(full.get("text") or row.get("text") or "")
        new_title = inject(title, mapping)
        new_text = inject(text, mapping) if args.also_text else text

        title_diff = new_title != title
        text_diff = args.also_text and new_text != text
        if not title_diff and not text_diff:
            print(f"OK  #{mid} (no emoji changes) title={title!r}")
            continue

        print(f"CHG #{mid}")
        if title_diff:
            print(f"  title: {title!r}")
            print(f"      -> {new_title!r}")
        if text_diff:
            print(f"  text len {len(text)} -> {len(new_text)}")

        if args.dry_run:
            continue
        if title_diff:
            update_title(args.bot_id, mid, new_title, secret, token)
            changed_titles += 1
            time.sleep(args.sleep)
        if text_diff:
            update_text(args.bot_id, mid, new_text, secret, token)
            changed_texts += 1
            time.sleep(args.sleep)

    print(f"done titles_updated={changed_titles} texts_updated={changed_texts}")
    print("NOTE: for premium emoji to render, message format must be HTML in BOT-T UI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
