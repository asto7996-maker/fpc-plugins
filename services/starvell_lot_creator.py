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
    BUILTIN_TELEGRAM_SUBSCRIBERS,
    DEFAULT_DELIVERY_TIME,
    PARSER_BUILD,
    apply_builtin_category_defaults,
    build_minimal_create_payload,
    build_offer_attributes,
    build_offer_update_payload,
    draft_attribute_count,
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
    """Формирует полный payload (описание + атрибуты) для create/update."""
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

    basic_attributes, numeric_attributes = build_offer_attributes(
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

    resolved_game_id = int(game_id) if game_id else 0
    if not resolved_game_id and int(category_id) == 175:
        resolved_game_id = 14

    payload: dict[str, Any] = {
        "type": offer_type,
        "categoryId": int(category_id),
        "price": format_starvell_api_price(price),
        "availability": AVAILABILITY_LOT,
        "isActive": True,
        "autoDelivery": bool(auto_delivery),
        "instantDelivery": False,
        "deliveryTime": DEFAULT_DELIVERY_TIME,
        "descriptions": descriptions,
    }
    if resolved_game_id:
        payload["gameId"] = resolved_game_id
    if basic_attributes:
        payload["basicAttributes"] = basic_attributes
    if numeric_attributes:
        payload["numericAttributes"] = numeric_attributes
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
    logger.info("full payload attrs: %s sub=%s", payload_attribute_stats(payload), sub_category_id)

    return payload


def offer_admin_url(offer_id: str | int, public_id: str | None = None) -> str:
    oid = str(public_id or offer_id or "").strip()
    if not oid:
        return f"{BASE_URL}/"
    return f"{BASE_URL}/offers/{oid}"


def _is_attribute_error(message: str) -> bool:
    text = (message or "").lower()
    return "атрибут" in text


def _create_attempts(
    full_payload: dict[str, Any],
    *,
    brief_ru: str,
    game_id: int,
    category_id: int,
) -> list[tuple[str, dict[str, Any], str]]:
    """Варианты минимального create: (name, body, finalize_mode)."""
    attempts: list[tuple[str, dict[str, Any], str]] = [
        (
            "minimal-unified",
            build_minimal_create_payload(
                full_payload,
                brief_ru=brief_ru,
                game_id=game_id,
                attr_mode="unified",
            ),
            "unified",
        ),
        (
            "minimal-basic",
            build_minimal_create_payload(
                full_payload,
                brief_ru=brief_ru,
                game_id=game_id,
                attr_mode="basic",
            ),
            "basic",
        ),
        (
            "minimal-no-attrs",
            build_minimal_create_payload(
                full_payload,
                brief_ru=brief_ru,
                game_id=game_id,
                include_attributes=False,
                attr_mode="none",
            ),
            "none",
        ),
        (
            "minimal-empty",
            build_minimal_create_payload(
                full_payload,
                brief_ru=brief_ru,
                game_id=game_id,
                include_attributes=False,
                explicit_empty_attrs=True,
                attr_mode="basic",
            ),
            "basic",
        ),
    ]

    if int(category_id) == 175:
        builtin_body = build_minimal_create_payload(
            {
                **full_payload,
                "categoryId": 175,
                "subCategoryId": int(BUILTIN_TELEGRAM_SUBSCRIBERS["sub_category_id"]),
                "basicAttributes": list(BUILTIN_TELEGRAM_SUBSCRIBERS["basic_attributes"]),
            },
            brief_ru=brief_ru,
            game_id=game_id or 14,
            attr_mode="basic",
        )
        attempts.append(("minimal-builtin", builtin_body, "basic"))

    seen: set[str] = set()
    unique: list[tuple[str, dict[str, Any], str]] = []
    for name, body, mode in attempts:
        key = payload_attribute_stats(body) + f"|{mode}"
        if key in seen:
            continue
        seen.add(key)
        unique.append((name, body, mode))
    return unique


async def _apply_full_content(
    api: StarvellAPI,
    offer_id: str | int,
    full_payload: dict[str, Any],
) -> None:
    """Фаза 2: полное описание и postPayment через partial-update."""
    update_body = build_offer_update_payload(full_payload)
    if not update_body:
        return
    oid = str(offer_id)
    try:
        await api.partial_update_offer(oid, update_body)
        logger.info("partial_update applied for offer %s", oid[:12])
    except StarvellAPIError as exc:
        logger.warning("partial_update failed (%s), trying full update", exc)
        await api.update_offer(oid, {**full_payload, **update_body})


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
    """Создаёт лот на Starvell (минимальный create → partial-update) и возвращает ответ."""
    _ = template_offer_id

    full_payload = await build_create_payload(
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

    brief_ru = (
        ((full_payload.get("descriptions") or {}).get("rus") or {}).get("briefDescription")
        or lot.title
        or "Лот"
    )
    resolved_game_id = int(game_id or full_payload.get("gameId") or 0)
    sub_category_id = full_payload.get("subCategoryId")

    try:
        draft = await api.fetch_offer_draft(
            category_id,
            sub_category_id=int(sub_category_id) if sub_category_id else None,
            game_slug=game_slug,
            category_slug=category_slug,
        )
        draft_count = draft_attribute_count(draft)
        if draft_count > 5:
            logger.warning(
                "offer draft has %s attrs (category=%s sub=%s) — using minimal create",
                draft_count,
                category_id,
                sub_category_id,
            )
    except Exception as exc:
        logger.debug("fetch_offer_draft: %s", exc)

    async def _send(body: dict[str, Any], finalize_mode: str) -> dict[str, Any]:
        return await api.create_offer(
            body,
            category_id=category_id,
            game_slug=game_slug,
            category_slug=category_slug,
            finalize_mode=finalize_mode,
        )

    attempts = _create_attempts(
        full_payload,
        brief_ru=str(brief_ru),
        game_id=resolved_game_id,
        category_id=category_id,
    )

    errors: list[str] = []
    result: dict[str, Any] | None = None
    winning_body: dict[str, Any] = attempts[0][1]
    winning_name = attempts[0][0]

    for name, body, mode in attempts:
        stats = payload_attribute_stats(body)
        try:
            result = await _send(body, mode)
            winning_body = body
            winning_name = name
            if name != attempts[0][0]:
                logger.info("create_offer succeeded on retry %s (%s)", name, stats)
            break
        except StarvellAPIError as exc:
            err_line = f"{name}: {stats} → {exc}"
            errors.append(err_line)
            logger.warning("create_offer attempt failed: %s", err_line)
            if not _is_attribute_error(str(exc)):
                raise StarvellAPIError(
                    exc.status,
                    f"{exc}\n\n📊 Отправлено: {stats}",
                    exc.body,
                ) from exc

    if result is None:
        last_stats = payload_attribute_stats(attempts[-1][1]) if attempts else ""
        raise StarvellAPIError(
            400,
            (
                f"Starvell отклонил все варианты payload ({PARSER_BUILD}).\n\n"
                f"📊 Последняя попытка: {last_stats}\n\n"
                + "\n".join(errors[-5:])
            ),
            {},
        )

    offer_id = result.get("id") or result.get("offerId")
    public_id = result.get("publicId")
    if offer_id and winning_name.startswith("minimal"):
        try:
            await _apply_full_content(api, offer_id, full_payload)
        except Exception as exc:
            logger.warning("post-create content update failed for %s: %s", offer_id, exc)

    return {
        "offer_id": offer_id,
        "public_id": public_id,
        "url": offer_admin_url(offer_id or "", public_id),
        "payload": winning_body,
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
