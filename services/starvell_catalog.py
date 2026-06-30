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
        "filters": category.get("filters") or [],
        "numericFilters": category.get("numericFilters") or [],
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

    service_type = _norm(hint_texts[0] if hint_texts else "")
    if service_type:
        for sub in subs:
            if not isinstance(sub, dict):
                continue
            name_n = _norm(str(sub.get("name") or ""))
            if name_n and (name_n in service_type or service_type in name_n):
                return sub

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


MAX_NUMERIC_ATTRIBUTES = 50
PARSER_BUILD = "attrs-v3"

# FunPay Telegram «Подписчики» → Starvell category 175 / sub 634 (fallback без каталога)
BUILTIN_TELEGRAM_SUBSCRIBERS: dict[str, Any] = {
    "category_id": 175,
    "sub_category_id": 634,
    "game_slug": "telegram",
    "category_slug": "services",
    "basic_attributes": [
        {
            "id": "e07ea24f-a7f4-4d2f-b0b6-cf54f4523590",
            "optionId": "c3d131de-d283-485b-95ae-faff681d67b1",
        },
    ],
}


def resolve_option_filters(
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Option-фильтры: подкатегория, иначе категория (как на сайте Starvell)."""
    if subcategory:
        subs = [f for f in (subcategory.get("filters") or []) if isinstance(f, dict)]
        if subs:
            return subs
    return [f for f in (catalog.get("filters") or []) if isinstance(f, dict)]


def resolve_numeric_filters(
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Числовые фильтры: подкатегория, иначе категория."""
    if subcategory:
        sub_nf = [f for f in (subcategory.get("numericFilters") or []) if isinstance(f, dict)]
        if sub_nf:
            return sub_nf
    return [f for f in (catalog.get("numericFilters") or []) if isinstance(f, dict)]


def _option_filter_ids(subcategory: dict[str, Any] | None, catalog: dict[str, Any] | None = None) -> set[str]:
    filters = resolve_option_filters(catalog or {}, subcategory)
    return {str(filt["id"]) for filt in filters if filt.get("id")}


def _pick_option_for_filter(filt: dict[str, Any], hint_text: str) -> dict[str, Any] | None:
    options = [
        o for o in (filt.get("options") or [])
        if isinstance(o, dict) and not o.get("redirectSlug") and not o.get("isHidden")
    ]
    if not options:
        return None
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
    return chosen


def build_basic_attributes(
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
    *,
    hint_text: str = "",
) -> list[dict[str, Any]]:
    """Option-атрибуты только из каталога Starvell (без шаблона лота)."""
    result: list[dict[str, Any]] = []
    for filt in resolve_option_filters(catalog, subcategory):
        chosen = _pick_option_for_filter(filt, hint_text)
        if chosen and filt.get("id"):
            result.append({"id": filt["id"], "optionId": chosen["id"]})
    return result


def build_numeric_attributes(
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Числовые атрибуты только из numericFilters каталога."""
    filters = resolve_numeric_filters(catalog, subcategory)
    if not filters:
        return []

    result: list[dict[str, Any]] = []
    for filt in filters[:MAX_NUMERIC_ATTRIBUTES]:
        filt_id = str(filt["id"])
        raw_value = (filt.get("range") or {}).get("min", 1)
        try:
            result.append({"id": filt_id, "numericValue": float(raw_value)})
        except (TypeError, ValueError):
            continue
    return result


def sanitize_create_attributes(
    payload: dict[str, Any],
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Жёсткая очистка атрибутов перед POST /api/offers/create.
    Starvell отклоняет >50 числовых — убираем всё лишнее из шаблона/мусора.
    """
    allowed_option = _option_filter_ids(subcategory, catalog)
    allowed_numeric = {
        str(f["id"]) for f in resolve_numeric_filters(catalog, subcategory) if f.get("id")
    }

    basic: list[dict[str, Any]] = []
    seen_option: set[str] = set()
    for item in payload.get("basicAttributes") or []:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "")
        if aid not in allowed_option or not item.get("optionId") or "numericValue" in item:
            continue
        if aid in seen_option:
            continue
        seen_option.add(aid)
        basic.append({"id": aid, "optionId": item["optionId"]})

    numeric: list[dict[str, Any]] = []
    seen_numeric: set[str] = set()
    for source_key in ("attributes", "numericAttributes"):
        for item in payload.get(source_key) or []:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "")
            if aid not in allowed_numeric or item.get("numericValue") is None:
                continue
            if aid in seen_numeric:
                continue
            try:
                seen_numeric.add(aid)
                numeric.append({"id": aid, "numericValue": float(item["numericValue"])})
            except (TypeError, ValueError):
                continue

    cleaned = dict(payload)
    cleaned["basicAttributes"] = basic
    cleaned.pop("numericAttributes", None)
    if numeric:
        cleaned["attributes"] = numeric[:MAX_NUMERIC_ATTRIBUTES]
    else:
        cleaned.pop("attributes", None)
    return cleaned


def finalize_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Последняя линия защиты: только разрешённые поля, без attributes/numericAttributes.
    basicAttributes — только {id, optionId}, без numericValue.
    """
    allowed_keys = (
        "type",
        "categoryId",
        "subCategoryId",
        "price",
        "availability",
        "isActive",
        "autoDelivery",
        "instantDelivery",
        "descriptions",
        "goods",
        "basicAttributes",
        "postPaymentMessage",
    )
    out: dict[str, Any] = {k: payload[k] for k in allowed_keys if k in payload}

    basic: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in out.get("basicAttributes") or []:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "")
        oid = item.get("optionId")
        if not aid or not oid or "numericValue" in item or aid in seen:
            continue
        seen.add(aid)
        basic.append({"id": aid, "optionId": str(oid)})
    out["basicAttributes"] = basic[:MAX_NUMERIC_ATTRIBUTES]

    numeric_attrs: list[dict[str, Any]] = []
    seen_num: set[str] = set()
    for item in payload.get("attributes") or []:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "")
        if not aid or item.get("numericValue") is None or item.get("optionId") or aid in seen_num:
            continue
        try:
            seen_num.add(aid)
            numeric_attrs.append({"id": aid, "numericValue": float(item["numericValue"])})
        except (TypeError, ValueError):
            continue
    if numeric_attrs and len(numeric_attrs) <= 10:
        out["attributes"] = numeric_attrs[:MAX_NUMERIC_ATTRIBUTES]

    if not out.get("goods"):
        out["goods"] = []

    return out


def apply_builtin_category_defaults(
    payload: dict[str, Any],
    *,
    category_id: int,
    sub_category_id: int | None,
) -> dict[str, Any]:
    """Жёсткий fallback для Telegram SMM, если каталог недоступен."""
    builtin = BUILTIN_TELEGRAM_SUBSCRIBERS
    if int(category_id) != int(builtin["category_id"]):
        return payload
    if sub_category_id and int(sub_category_id) != int(builtin["sub_category_id"]):
        return payload
    if payload.get("basicAttributes"):
        return payload
    result = dict(payload)
    result["subCategoryId"] = int(builtin["sub_category_id"])
    result["basicAttributes"] = list(builtin["basic_attributes"])
    return result


def payload_attribute_stats(payload: dict[str, Any]) -> str:
    basic = len(payload.get("basicAttributes") or [])
    attrs = len(payload.get("attributes") or [])
    numeric_legacy = len(payload.get("numericAttributes") or [])
    return f"basic={basic} attributes={attrs} numericLegacy={numeric_legacy} build={PARSER_BUILD}"


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
