"""Метаданные категорий Starvell (подкатегории, фильтры)."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("starvell.catalog")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_CATALOG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 1800

SUBCATEGORY_HINTS: list[tuple[str, str]] = [
    ("подписчик", "подписчик"),
    ("subscriber", "подписчик"),
    ("followers", "подписчик"),
    ("просмотр", "просмотр"),
    ("views", "просмотр"),
    ("реакци", "реакци"),
    ("reaction", "реакци"),
    ("репост", "репост"),
    ("repost", "репост"),
    ("реферал", "реферал"),
    ("referral", "реферал"),
    ("коммент", "коммент"),
    ("comment", "коммент"),
    ("голос", "голос"),
    ("vote", "голос"),
    ("буст", "буст"),
    ("boost", "буст"),
    ("реклам", "реклам"),
    ("дизайн", "дизайн"),
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


async def fetch_category_catalog(game_slug: str, category_slug: str) -> dict[str, Any]:
    """Загружает category + subCategories с публичной страницы Starvell."""
    game_slug = (game_slug or "").strip().strip("/")
    category_slug = (category_slug or "").strip().strip("/")
    if not game_slug or not category_slug:
        return {}

    cache_key = f"{game_slug}/{category_slug}"
    now = time.time()
    cached = _CATALOG_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    url = f"https://starvell.com/{game_slug}/{category_slug}"
    try:
        async with httpx.AsyncClient(
            timeout=25.0,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning("fetch_category_catalog %s: %s", cache_key, exc)
        return {}

    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
        category = (data.get("props") or {}).get("pageProps", {}).get("category") or {}
    except Exception as exc:
        logger.warning("parse category json %s: %s", cache_key, exc)
        return {}

    result = {
        "id": category.get("id"),
        "name": category.get("name"),
        "slug": category.get("slug"),
        "offerType": category.get("offerType") or "LOT",
        "isBriefDescriptionEnabled": bool(category.get("isBriefDescriptionEnabled", True)),
        "autoDelivery": category.get("autoDelivery"),
        "game_slug": game_slug,
        "category_slug": category_slug,
        "subCategories": category.get("subCategories") or [],
    }
    _CATALOG_CACHE[cache_key] = (now, result)
    return result


def _score_subcategory(name: str, hints: set[str]) -> float:
    name_n = _norm(name)
    if not name_n:
        return 0.0
    for hint in hints:
        if hint in name_n or name_n in hint:
            return 1.0
    return 0.0


def collect_subcategory_hints(*texts: str) -> set[str]:
    blob = _norm(" ".join(t for t in texts if t))
    hints: set[str] = set()
    if not blob:
        return hints
    for needle, label in SUBCATEGORY_HINTS:
        if needle in blob:
            hints.add(_norm(label))
    words = re.split(r"[\s,;/|]+", blob)
    for word in words:
        if len(word) >= 4:
            hints.add(word)
    return hints


def pick_subcategory(catalog: dict[str, Any], *hint_texts: str) -> dict[str, Any] | None:
    subs = catalog.get("subCategories") or []
    if not subs:
        return None
    hints = collect_subcategory_hints(*hint_texts)
    best: dict[str, Any] | None = None
    best_score = 0.0
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        name = str(sub.get("name") or "")
        score = _score_subcategory(name, hints)
        if score > best_score:
            best_score = score
            best = sub
    if best:
        return best
    return subs[0] if len(subs) == 1 else None


def build_basic_attributes(
    subcategory: dict[str, Any] | None,
    *,
    template_offer: dict[str, Any] | None = None,
    hint_text: str = "",
) -> list[dict[str, Any]]:
    if template_offer:
        attrs: list[dict[str, Any]] = []
        for attr in template_offer.get("attributes") or template_offer.get("basicAttributes") or []:
            if not isinstance(attr, dict):
                continue
            if attr.get("optionId"):
                attrs.append({"id": attr["id"], "optionId": attr["optionId"]})
            elif attr.get("numericValue") is not None:
                attrs.append({"id": attr["id"], "numericValue": attr["numericValue"]})
        if attrs:
            return attrs

    if not subcategory:
        return []

    text = _norm(hint_text)
    result: list[dict[str, Any]] = []
    for filt in subcategory.get("filters") or []:
        if not isinstance(filt, dict):
            continue
        options = [
            o for o in (filt.get("options") or [])
            if isinstance(o, dict) and not o.get("redirectSlug") and not o.get("isHidden")
        ]
        if not options:
            continue
        chosen = options[-1]
        for opt in options:
            name = _norm(str(opt.get("nameRu") or ""))
            if "обычн" in name or "regular" in name:
                chosen = opt
                break
        text_l = _norm(hint_text)
        if any(k in text_l for k in ("premium", "прем", "telegram premium", "тг прем")):
            for opt in options:
                name = _norm(str(opt.get("nameRu") or ""))
                if "premium" in name or "прем" in name:
                    chosen = opt
                    break
        else:
            for opt in options:
                name = _norm(str(opt.get("nameRu") or ""))
                if name and name in text_l:
                    chosen = opt
                    break
        result.append({"id": filt["id"], "optionId": chosen["id"]})
    return result


async def resolve_slugs_for_category(category_id: int, game_id: int = 0) -> tuple[str, str]:
    """Возвращает (game_slug, category_slug) по ID категории."""
    from services.category_mapper import search_starvell_games

    keywords = ["telegram"] if category_id == 175 else []
    if game_id == 14:
        keywords.insert(0, "telegram")
    keywords.extend(["", str(category_id)])

    seen: set[int] = set()
    for kw in keywords:
        if not kw and kw != "":
            continue
        for game in await search_starvell_games(kw or "a"):
            gid = game.get("id")
            if gid in seen:
                continue
            seen.add(gid)
            if game_id and int(gid) != int(game_id):
                continue
            for cat in game.get("categories") or []:
                if int(cat.get("id") or 0) == int(category_id):
                    return str(game.get("slug") or ""), str(cat.get("slug") or "")
    if category_id == 175:
        return "telegram", "services"
    return "", ""
