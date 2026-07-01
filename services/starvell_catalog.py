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
PARSER_BUILD = "attrs-v7.3"

MIN_DELIVERY_FROM_MINUTES = 10

# Подкатегории Telegram (175), где на лотах есть deliveryTime (634 — Подписчики).
DELIVERY_TIME_SUBCATEGORY_IDS: dict[int, set[int]] = {
    175: {634},
}

DEFAULT_DELIVERY_TIME: dict[str, Any] = {
    "from": {"unit": "MINUTES", "value": MIN_DELIVERY_FROM_MINUTES},
    "to": {"unit": "HOURS", "value": 6},
}

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


def build_offer_attributes(
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
    *,
    hint_text: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Option → basicAttributes, numeric → numericAttributes (схема POST /offers/create)."""
    return (
        build_basic_attributes(catalog, subcategory, hint_text=hint_text),
        build_numeric_attributes(catalog, subcategory),
    )


def sanitize_create_attributes(
    payload: dict[str, Any],
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
) -> dict[str, Any]:
    """Очищает атрибуты: basicAttributes + numericAttributes, без attributes."""
    allowed_option = _option_filter_ids(subcategory, catalog)
    allowed_numeric = {
        str(f["id"]) for f in resolve_numeric_filters(catalog, subcategory) if f.get("id")
    }

    basic: list[dict[str, Any]] = []
    numeric: list[dict[str, Any]] = []
    seen_option: set[str] = set()
    seen_numeric: set[str] = set()

    def _scan(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "")
            if not aid:
                continue
            if item.get("optionId") and aid in allowed_option and "numericValue" not in item:
                if aid not in seen_option:
                    seen_option.add(aid)
                    basic.append({"id": aid, "optionId": str(item["optionId"])})
            elif item.get("numericValue") is not None and aid in allowed_numeric and not item.get("optionId"):
                if aid not in seen_numeric:
                    try:
                        seen_numeric.add(aid)
                        numeric.append({"id": aid, "numericValue": float(item["numericValue"])})
                    except (TypeError, ValueError):
                        pass

    _scan(payload.get("basicAttributes"))
    _scan(payload.get("attributes"))
    _scan(payload.get("numericAttributes"))

    cleaned = dict(payload)
    cleaned.pop("attributes", None)
    if basic:
        cleaned["basicAttributes"] = basic
    else:
        cleaned.pop("basicAttributes", None)
    if numeric:
        cleaned["numericAttributes"] = numeric[:10]
    else:
        cleaned.pop("numericAttributes", None)
    return cleaned


def compact_option_attributes(items: Any) -> list[dict[str, str]]:
    """Option-атрибуты в минимальном формате {id, optionId} (как на публичных лотах)."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "")
        oid = item.get("optionId")
        if not aid or not oid or aid in seen or item.get("numericValue") is not None:
            continue
        seen.add(aid)
        result.append({"id": aid, "optionId": str(oid)})
    return result


def _collect_create_option_numeric(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    basic: list[dict[str, Any]] = []
    numeric: list[dict[str, Any]] = []
    seen_option: set[str] = set()
    seen_numeric: set[str] = set()

    for source in (payload.get("basicAttributes"), payload.get("attributes")):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "")
            if not aid or aid in seen_option:
                continue
            if item.get("optionId") and "numericValue" not in item:
                seen_option.add(aid)
                basic.append({"id": aid, "optionId": str(item["optionId"])})

    for source in (payload.get("numericAttributes"), payload.get("attributes")):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "")
            if not aid or aid in seen_numeric or item.get("optionId"):
                continue
            if item.get("numericValue") is None:
                continue
            try:
                seen_numeric.add(aid)
                numeric.append({"id": aid, "numericValue": float(item["numericValue"])})
            except (TypeError, ValueError):
                pass
    return basic, numeric


def _create_base_fields(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "type",
        "categoryId",
        "gameId",
        "subCategoryId",
        "price",
        "availability",
        "isActive",
        "autoDelivery",
        "instantDelivery",
        "descriptions",
        "deliveryTime",
        "postPaymentMessage",
    )
    return {k: payload[k] for k in allowed_keys if k in payload}


def subcategory_supports_delivery_time(category_id: int | None, sub_category_id: int | None) -> bool:
    """Публичные лоты sub 632/633 без deliveryTime; sub 634 — с deliveryTime."""
    if not category_id or not sub_category_id:
        return False
    allowed = DELIVERY_TIME_SUBCATEGORY_IDS.get(int(category_id))
    return bool(allowed and int(sub_category_id) in allowed)


def normalize_delivery_time(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """from ≥10 мин, to ≥1; value — числа (как в API Starvell)."""
    base = raw if isinstance(raw, dict) else DEFAULT_DELIVERY_TIME
    from_block = dict(base.get("from") or DEFAULT_DELIVERY_TIME["from"])
    to_block = dict(base.get("to") or DEFAULT_DELIVERY_TIME["to"])

    from_unit = str(from_block.get("unit") or "MINUTES").upper()
    to_unit = str(to_block.get("unit") or "HOURS").upper()

    try:
        from_num = int(float(str(from_block.get("value") or "0").replace(",", ".")))
    except (TypeError, ValueError):
        from_num = 0
    try:
        to_num = int(float(str(to_block.get("value") or "0").replace(",", ".")))
    except (TypeError, ValueError):
        to_num = 0

    if from_unit == "MINUTES" and from_num < MIN_DELIVERY_FROM_MINUTES:
        from_num = MIN_DELIVERY_FROM_MINUTES
    elif from_num < 1:
        from_num = 1
    if to_num < 1:
        to_num = 1

    return {
        "from": {"unit": from_unit, "value": from_num},
        "to": {"unit": to_unit, "value": to_num},
    }


def _delivery_time_for_create(
    raw: Any,
    *,
    category_id: int | None = None,
    sub_category_id: int | None = None,
    include: bool = False,
) -> dict[str, Any] | None:
    """
    deliveryTime на create — только если include и подкатегория поддерживает (634).
    Для 633 (Просмотры) и др. поле не отправляем (на сайте null).
    """
    if not include:
        return None
    if not subcategory_supports_delivery_time(category_id, sub_category_id):
        return None
    if not isinstance(raw, dict):
        raw = DEFAULT_DELIVERY_TIME
    return normalize_delivery_time(raw)


def _filter_basic_attributes(
    basic: list[dict[str, Any]],
    catalog: dict[str, Any] | None,
    subcategory: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Option-атрибуты с проверкой id/optionId по каталогу (как submit на Starvell)."""
    allowed_option = _option_filter_ids(subcategory, catalog or {})
    option_map: dict[str, set[str]] = {}
    for filt in resolve_option_filters(catalog or {}, subcategory):
        fid = str(filt.get("id") or "")
        if not fid:
            continue
        option_map[fid] = {
            str(opt["id"])
            for opt in (filt.get("options") or [])
            if isinstance(opt, dict) and opt.get("id") and not opt.get("redirectSlug") and not opt.get("isHidden")
        }

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in basic:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "")
        oid = item.get("optionId")
        if not aid or not oid or aid in seen:
            continue
        if allowed_option and aid not in allowed_option:
            continue
        if option_map and aid in option_map and str(oid) not in option_map[aid]:
            continue
        seen.add(aid)
        result.append({"id": aid, "optionId": str(oid)})
    return result


def finalize_frontend_create_payload(
    payload: dict[str, Any],
    *,
    catalog: dict[str, Any] | None = None,
    subcategory: dict[str, Any] | None = None,
    brief_enabled: bool = True,
    force_empty_numeric: bool = True,
    include_delivery_time: bool = False,
) -> dict[str, Any]:
    """
    POST /offers/create — как submit формы Starvell (chunk 5661):
    basicAttributes + numericAttributes: [], goods: [], без attributes.
    autoDelivery=false для SMM (true включает goods-режим с десятками numeric attrs).
    """
    basic, numeric = _collect_create_option_numeric(payload)
    basic = _filter_basic_attributes(basic, catalog, subcategory)

    desc_rus = ((payload.get("descriptions") or {}).get("rus") or {})
    descriptions: dict[str, Any] = {
        "rus": {
            "description": desc_rus.get("description") or "",
        },
    }
    if brief_enabled and desc_rus.get("briefDescription"):
        descriptions["rus"]["briefDescription"] = desc_rus["briefDescription"]

    price_raw = payload.get("price")
    if price_raw is not None:
        price = str(price_raw).replace(",", ".")
    else:
        price = "0"

    out: dict[str, Any] = {
        "type": payload.get("type") or "LOT",
        "categoryId": payload.get("categoryId"),
        "price": price,
        "availability": payload.get("availability"),
        "isActive": payload.get("isActive", True),
        "autoDelivery": bool(payload.get("autoDelivery", False)),
        "instantDelivery": False,
        "goods": [],
        "descriptions": descriptions,
    }
    delivery = _delivery_time_for_create(
        payload.get("deliveryTime"),
        category_id=payload.get("categoryId"),
        sub_category_id=payload.get("subCategoryId"),
        include=include_delivery_time,
    )
    if delivery is not None:
        out["deliveryTime"] = delivery
    if payload.get("gameId"):
        out["gameId"] = int(payload["gameId"])
    if payload.get("subCategoryId"):
        out["subCategoryId"] = int(payload["subCategoryId"])
    if payload.get("postPaymentMessage"):
        out["postPaymentMessage"] = payload["postPaymentMessage"]

    if basic:
        out["basicAttributes"] = basic[:20]

    if force_empty_numeric or numeric:
        out["numericAttributes"] = numeric[:10] if numeric and not force_empty_numeric else []
    return out


def finalize_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /offers/create: basicAttributes (+ numericAttributes при необходимости)."""
    out = _create_base_fields(payload)
    basic, numeric = _collect_create_option_numeric(payload)

    out.pop("attributes", None)
    if basic:
        out["basicAttributes"] = basic[:20]
    else:
        out.pop("basicAttributes", None)
    if numeric:
        out["numericAttributes"] = numeric[:10]
    else:
        out.pop("numericAttributes", None)
    return out


def finalize_unified_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """POST /offers/create: единый массив attributes (id + optionId), без legacy-полей."""
    out = _create_base_fields(payload)
    unified = compact_option_attributes(payload.get("attributes"))
    if not unified:
        unified = compact_option_attributes(payload.get("basicAttributes"))
    out.pop("basicAttributes", None)
    out.pop("numericAttributes", None)
    if unified:
        out["attributes"] = unified[:20]
    else:
        out.pop("attributes", None)
    return out


def build_minimal_create_payload(
    base: dict[str, Any],
    *,
    brief_ru: str,
    game_id: int = 0,
    include_post_payment: bool = False,
    include_attributes: bool = True,
    auto_delivery: bool = False,
    delivery_time: dict[str, Any] | None = None,
    include_delivery_time: bool = False,
    catalog: dict[str, Any] | None = None,
    subcategory: dict[str, Any] | None = None,
    brief_enabled: bool = True,
) -> dict[str, Any]:
    """Минимальный create по схеме веб-формы Starvell (короткое описание, без postPayment)."""
    brief = (brief_ru or "Лот").strip()[:100]
    if delivery_time is None:
        delivery_time = DEFAULT_DELIVERY_TIME
    raw: dict[str, Any] = {
        "type": base.get("type") or "LOT",
        "categoryId": base.get("categoryId"),
        "subCategoryId": base.get("subCategoryId"),
        "price": base.get("price"),
        "availability": base.get("availability", 999999),
        "isActive": base.get("isActive", True),
        "autoDelivery": auto_delivery,
        "instantDelivery": False,
        "deliveryTime": delivery_time,
        "descriptions": {
            "rus": {
                "briefDescription": brief,
                "description": brief,
            },
        },
    }
    if game_id:
        raw["gameId"] = int(game_id)
    elif base.get("gameId"):
        raw["gameId"] = int(base["gameId"])

    if include_attributes and base.get("basicAttributes"):
        raw["basicAttributes"] = list(base["basicAttributes"])

    if include_post_payment and base.get("postPaymentMessage"):
        raw["postPaymentMessage"] = base["postPaymentMessage"]

    return finalize_frontend_create_payload(
        raw,
        catalog=catalog,
        subcategory=subcategory,
        brief_enabled=brief_enabled,
        force_empty_numeric=True,
        include_delivery_time=include_delivery_time,
    )


def build_offer_update_payload(full: dict[str, Any]) -> dict[str, Any]:
    """Тело partial-update после успешного create: полное описание и SMM-сообщение."""
    out: dict[str, Any] = {}
    if full.get("descriptions"):
        out["descriptions"] = full["descriptions"]
    if full.get("postPaymentMessage"):
        out["postPaymentMessage"] = full["postPaymentMessage"]
    if subcategory_supports_delivery_time(full.get("categoryId"), full.get("subCategoryId")):
        out["deliveryTime"] = normalize_delivery_time(full.get("deliveryTime"))
    unified = compact_option_attributes(full.get("attributes"))
    if not unified:
        unified = compact_option_attributes(full.get("basicAttributes"))
    if unified:
        out["attributes"] = unified
    return out


def strip_all_attributes(payload: dict[str, Any]) -> dict[str, Any]:
    """Payload без любых полей атрибутов — fallback для Starvell create."""
    result = finalize_create_payload(payload)
    result.pop("basicAttributes", None)
    result.pop("numericAttributes", None)
    result.pop("attributes", None)
    return result


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
    result.pop("attributes", None)
    result.pop("numericAttributes", None)
    result["subCategoryId"] = int(builtin["sub_category_id"])
    result["basicAttributes"] = list(builtin["basic_attributes"])
    return result


def payload_attribute_stats(payload: dict[str, Any]) -> str:
    basic = len(payload.get("basicAttributes") or [])
    numeric = len(payload.get("numericAttributes") or [])
    attrs = len(payload.get("attributes") or [])
    desc = ((payload.get("descriptions") or {}).get("rus") or {}).get("description") or ""
    desc_len = len(str(desc))
    keys = ",".join(sorted(k for k in payload.keys() if not k.startswith("_")))
    cat = payload.get("categoryId")
    sub = payload.get("subCategoryId")
    ad = payload.get("autoDelivery")
    goods = len(payload.get("goods") or [])
    dt = payload.get("deliveryTime")
    dt_flag = "yes" if dt else "no"
    return (
        f"cat={cat} sub={sub} autoDel={ad} dt={dt_flag} goods={goods} "
        f"basic={basic} numeric={numeric} attributes={attrs} desc={desc_len} "
        f"keys=[{keys}] build={PARSER_BUILD}"
    )


def draft_attribute_count(draft: dict[str, Any]) -> int:
    if not isinstance(draft, dict):
        return 0
    for key in ("attributes", "basicAttributes", "numericAttributes"):
        items = draft.get(key)
        if isinstance(items, list) and items:
            return len(items)
    offer = draft.get("offer") or draft.get("draft") or {}
    if isinstance(offer, dict):
        return draft_attribute_count(offer)
    return 0


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
