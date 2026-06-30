"""Сборка payload и создание лота на Starvell из распарсенного FunPay лота."""

from __future__ import annotations

import logging
from typing import Any

from services.funpay_parser import (
    DEFAULT_EXECUTION_TIME,
    DEFAULT_SMM_AFTER_PAYMENT,
    ParsedLot,
    build_starvell_package,
    strip_service_id,
)
from services.price_utils import format_price_display, format_starvell_api_price
from services.starvell_catalog import (
    build_basic_attributes,
    build_numeric_attributes,
    fetch_category_catalog,
    pick_subcategory,
    resolve_slugs_for_category,
)
from starvell_api import StarvellAPI, BASE_URL, StarvellAPIError

logger = logging.getLogger("starvell.lot_creator")

AVAILABILITY_LOT = 999999
EN_SEPARATOR = "\n\n━━━━━━━━━━━━━━━━━━\n🇬🇧 English\n\n"
BRIEF_MAX = 100
FULL_MAX = 4800


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def build_bilingual_full(ru: str, en: str) -> str:
    ru = (ru or "").strip()
    en = (en or "").strip()
    if not en or en == ru:
        return _truncate(ru, FULL_MAX)
    combined = f"{ru}{EN_SEPARATOR}{en}"
    return _truncate(combined, FULL_MAX)


def _hint_blob(lot: ParsedLot) -> str:
    return " ".join(filter(None, [
        lot.funpay_service_type,
        lot.title,
        lot.brief_ru,
        lot.full_ru,
        lot.funpay_category_title,
    ]))


async def build_create_payload(
    lot: ParsedLot,
    *,
    is_smm: bool,
    service_id: int | None,
    price: str,
    category_id: int,
    game_id: int = 0,
    game_slug: str = "",
    category_slug: str = "",
    template_offer: dict[str, Any] | None = None,
    auto_delivery: bool = True,
) -> dict[str, Any]:
    """Формирует тело POST /api/offers/create по схеме Starvell."""
    sections = build_starvell_package(
        lot,
        is_smm=is_smm,
        service_id=service_id,
        auto_delivery=auto_delivery,
    )
    brief_ru = _truncate(strip_service_id(sections.get("brief_ru") or lot.title), BRIEF_MAX)
    full_ru = strip_service_id(sections.get("full_ru") or brief_ru)
    full_en = strip_service_id(sections.get("full_en") or lot.full_en or "")
    if is_smm and service_id:
        id_line = f"ID: {service_id}"
        if id_line not in full_ru:
            full_ru = f"{full_ru.rstrip()}\n\n{id_line}".strip()
        if full_en and id_line not in full_en:
            full_en = f"{full_en.rstrip()}\n\n{id_line}".strip()

    full_description = build_bilingual_full(full_ru, full_en)
    if DEFAULT_EXECUTION_TIME not in full_description:
        full_description = f"{full_description.rstrip()}\n\n{DEFAULT_EXECUTION_TIME}".strip()
        full_description = _truncate(full_description, FULL_MAX)

    if not game_slug or not category_slug:
        game_slug, category_slug = await resolve_slugs_for_category(category_id, game_id)

    catalog = await fetch_category_catalog(game_slug, category_slug) if game_slug and category_slug else {}
    offer_type = str(catalog.get("offerType") or "LOT")

    sub_category_id = None
    if template_offer:
        sub_category_id = template_offer.get("subCategoryId") or (
            (template_offer.get("subCategory") or {}).get("id")
        )

    subcategory = None
    subs = catalog.get("subCategories") or []
    if subs:
        if sub_category_id:
            subcategory = next((s for s in subs if int(s.get("id") or 0) == int(sub_category_id)), None)
        if not subcategory:
            subcategory = pick_subcategory(
                catalog,
                lot.funpay_service_type,
                lot.title,
                lot.brief_ru,
                lot.funpay_category_title,
            )
        if subcategory:
            sub_category_id = subcategory.get("id")
        elif len(subs) == 1:
            sub_category_id = subs[0].get("id")
            subcategory = subs[0]

    if subs and not sub_category_id:
        names = ", ".join(str(s.get("name") or s.get("id")) for s in subs[:8])
        raise StarvellAPIError(
            400,
            f"Не удалось определить подкатегорию Starvell. Доступны: {names}",
            {},
        )

    basic_attributes = build_basic_attributes(
        subcategory,
        template_offer=template_offer,
        hint_text=_hint_blob(lot),
    )
    numeric_attributes = build_numeric_attributes(
        subcategory,
        template_offer=template_offer,
    )

    brief_enabled = catalog.get("isBriefDescriptionEnabled", True)
    descriptions: dict[str, Any] = {
        "rus": {
            "description": full_description,
        },
    }
    if brief_enabled and brief_ru:
        descriptions["rus"]["briefDescription"] = brief_ru

    payload: dict[str, Any] = {
        "type": offer_type,
        "categoryId": int(category_id),
        "price": format_starvell_api_price(price),
        "availability": AVAILABILITY_LOT,
        "isActive": True,
        "autoDelivery": bool(auto_delivery),
        "instantDelivery": False,
        "descriptions": descriptions,
        "goods": [],
        "basicAttributes": basic_attributes,
    }
    if sub_category_id:
        payload["subCategoryId"] = int(sub_category_id)
    if numeric_attributes:
        payload["numericAttributes"] = numeric_attributes
    if is_smm:
        payload["postPaymentMessage"] = DEFAULT_SMM_AFTER_PAYMENT

    return payload


def offer_admin_url(offer_id: str | int, public_id: str | None = None) -> str:
    oid = str(public_id or offer_id or "").strip()
    if not oid:
        return f"{BASE_URL}/"
    return f"{BASE_URL}/offers/{oid}"


async def create_lot_from_parsed(
    api: StarvellAPI,
    lot: ParsedLot,
    *,
    is_smm: bool,
    service_id: int | None,
    price: str,
    category_id: int,
    game_id: int = 0,
    game_slug: str = "",
    category_slug: str = "",
    template_offer_id: str | int | None = None,
    auto_delivery: bool = True,
) -> dict[str, Any]:
    """Создаёт лот на Starvell и возвращает ответ API + ссылку."""
    template_offer: dict[str, Any] | None = None
    if template_offer_id:
        try:
            template_offer = await api.fetch_offer(str(template_offer_id))
        except Exception as exc:
            logger.warning("template offer %s: %s", template_offer_id, exc)

    payload = await build_create_payload(
        lot,
        is_smm=is_smm,
        service_id=service_id,
        price=price,
        category_id=category_id,
        game_id=game_id,
        game_slug=game_slug,
        category_slug=category_slug,
        template_offer=template_offer,
        auto_delivery=auto_delivery,
    )
    logger.debug("create_offer payload: %s", payload)
    result = await api.create_offer(payload, category_id=category_id, game_slug=game_slug, category_slug=category_slug)
    offer_id = result.get("id") or result.get("offerId")
    public_id = result.get("publicId")
    return {
        "offer_id": offer_id,
        "public_id": public_id,
        "url": offer_admin_url(offer_id or "", public_id),
        "payload": payload,
        "raw": result,
    }


def format_created_message(
    *,
    title: str,
    url: str,
    price: str,
    category_id: int,
    is_smm: bool,
    service_id: int | None,
) -> str:
    lines = [
        "✅ <b>Лот создан на Starvell</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📌 {title}",
        f"💰 Цена: <code>{format_price_display(price)}</code> ₽",
        f"📦 Наличие: <code>{AVAILABILITY_LOT}</code>",
        f"📁 Категория: <code>{category_id}</code>",
    ]
    if is_smm and service_id:
        lines.append(f"🆔 VexBoost ID: <code>{service_id}</code>")
    lines += [
        "🤖 Автоматизированная доставка: включена",
        "🇷🇺 + 🇬🇧 Описание: RU и EN в одном поле",
        "",
        f'🔗 <a href="{url}">Открыть лот на Starvell</a>',
    ]
    return "\n".join(lines)
