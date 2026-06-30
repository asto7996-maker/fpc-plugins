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
    PARSER_BUILD,
    apply_builtin_category_defaults,
    build_offer_attributes,
    fetch_category_catalog,
    finalize_create_payload,
    payload_attribute_stats,
    pick_subcategory,
    resolve_slugs_for_category,
    sanitize_create_attributes,
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
    subcategory = None
    subs = catalog.get("subCategories") or []
    if subs:
        subcategory = pick_subcategory(
            catalog,
            lot.funpay_service_type,
            lot.title,
            lot.brief_ru,
            lot.funpay_category_title,
        )
        if not subcategory and int(category_id) == 175:
            subcategory = next(
                (s for s in subs if int(s.get("id") or 0) == 634),
                None,
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

    offer_attributes = build_offer_attributes(
        catalog,
        subcategory,
        hint_text=_hint_blob(lot),
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
    }
    if offer_attributes:
        payload["attributes"] = offer_attributes
    if sub_category_id:
        payload["subCategoryId"] = int(sub_category_id)
    if is_smm:
        payload["postPaymentMessage"] = DEFAULT_SMM_AFTER_PAYMENT

    payload = sanitize_create_attributes(payload, catalog, subcategory)
    payload = apply_builtin_category_defaults(
        payload,
        category_id=int(category_id),
        sub_category_id=int(sub_category_id) if sub_category_id else None,
    )
    payload = finalize_create_payload(payload)
    logger.info("create payload attrs: %s sub=%s", payload_attribute_stats(payload), sub_category_id)

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
    _ = template_offer_id  # шаблон больше не используется — только каталог Starvell

    payload = await build_create_payload(
        lot,
        is_smm=is_smm,
        service_id=service_id,
        price=price,
        category_id=category_id,
        game_id=game_id,
        game_slug=game_slug,
        category_slug=category_slug,
        auto_delivery=auto_delivery,
    )
    logger.debug("create_offer payload: %s", payload)
    stats = payload_attribute_stats(payload)

    async def _send(body: dict[str, Any]) -> dict[str, Any]:
        return await api.create_offer(
            body,
            category_id=category_id,
            game_slug=game_slug,
            category_slug=category_slug,
        )

    try:
        result = await _send(payload)
    except StarvellAPIError as exc:
        err_text = str(exc)
        if int(category_id) == 175 and "Числовых" in err_text:
            from services.starvell_catalog import BUILTIN_TELEGRAM_SUBSCRIBERS

            retry_payload = finalize_create_payload({
                "type": payload.get("type") or "LOT",
                "categoryId": 175,
                "subCategoryId": int(BUILTIN_TELEGRAM_SUBSCRIBERS["sub_category_id"]),
                "price": payload.get("price"),
                "availability": payload.get("availability", AVAILABILITY_LOT),
                "isActive": True,
                "autoDelivery": payload.get("autoDelivery", True),
                "instantDelivery": False,
                "descriptions": payload.get("descriptions") or {},
                "goods": [],
                "attributes": list(BUILTIN_TELEGRAM_SUBSCRIBERS["attributes"]),
                **({"postPaymentMessage": payload["postPaymentMessage"]} if payload.get("postPaymentMessage") else {}),
            })
            retry_stats = payload_attribute_stats(retry_payload)
            logger.warning("create_offer retry minimal telegram (%s): was %s", retry_stats, stats)
            try:
                result = await _send(retry_payload)
                payload = retry_payload
                stats = retry_stats
            except StarvellAPIError as retry_exc:
                logger.warning("create_offer retry failed (%s): %s", retry_stats, retry_exc)
                raise StarvellAPIError(
                    retry_exc.status,
                    f"{retry_exc}\n\n📊 Первая попытка: {stats}\n📊 Retry: {retry_stats}",
                    retry_exc.body,
                ) from retry_exc
        else:
            logger.warning("create_offer rejected (%s): %s", stats, exc)
            raise StarvellAPIError(
                exc.status,
                f"{exc}\n\n📊 Отправлено: {stats}",
                exc.body,
            ) from exc
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
