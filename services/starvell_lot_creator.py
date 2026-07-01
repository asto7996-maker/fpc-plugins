"""Сборка payload и создание лота на Starvell из распарсенного FunPay лота."""

from __future__ import annotations

import logging
from typing import Any

from services.funpay_parser import (
    DEFAULT_EXECUTION_TIME,
    DEFAULT_SMM_AFTER_PAYMENT,
    ParsedLot,
    build_starvell_package,
    compose_starvell_descriptions,
)
from services.price_utils import format_price_display, format_starvell_api_price
from services.starvell_catalog import (
    BUILTIN_TELEGRAM_SUBSCRIBERS,
    DEFAULT_DELIVERY_TIME,
    PARSER_BUILD,
    apply_builtin_category_defaults,
    build_minimal_create_payload,
    build_offer_attributes,
    build_offer_content_update_payload,
    build_offer_partial_update_payload,
    compact_option_attributes,
    draft_attribute_count,
    fetch_category_catalog,
    payload_attribute_stats,
    pick_subcategory,
    resolve_slugs_for_category,
    sanitize_create_attributes,
    subcategory_supports_delivery_time,
    subcategory_supports_min_order,
)
from services.vexboost_service import VexBoostServiceError, VexBoostServiceInfo, fetch_vexboost_service
from starvell_api import StarvellAPI, BASE_URL, StarvellAPIError

logger = logging.getLogger("starvell.lot_creator")

AVAILABILITY_LOT = 999999
AVAILABILITY_CAP = 999999
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


def _resolve_availability(max_qty: int | None) -> int:
    if not max_qty or max_qty < 1:
        return AVAILABILITY_LOT
    return min(int(max_qty), AVAILABILITY_CAP)


def _coerce_vexboost_info(raw: Any) -> VexBoostServiceInfo | None:
    if raw is None:
        return None
    if isinstance(raw, VexBoostServiceInfo):
        return raw
    if isinstance(raw, dict) and raw.get("service_id"):
        min_qty = max(1, int(raw.get("min_qty") or 1))
        max_qty = max(min_qty, int(raw.get("max_qty") or min_qty))
        return VexBoostServiceInfo(
            service_id=int(raw["service_id"]),
            name=str(raw.get("name") or ""),
            min_qty=min_qty,
            max_qty=max_qty,
            rate=str(raw.get("rate") or ""),
            category=str(raw.get("category") or ""),
        )
    return None


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
    vexboost_info: VexBoostServiceInfo | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Полный payload для partial-update после create (описание, postPayment, атрибуты)."""
    _ = auto_delivery
    vexboost_info = _coerce_vexboost_info(vexboost_info)
    min_qty = vexboost_info.min_qty if vexboost_info else None
    max_qty = vexboost_info.max_qty if vexboost_info else None

    if is_smm and service_id and not vexboost_info:
        try:
            vexboost_info = await fetch_vexboost_service(service_id, db=db)
            min_qty = vexboost_info.min_qty
            max_qty = vexboost_info.max_qty
        except VexBoostServiceError as exc:
            raise StarvellAPIError(400, str(exc), {}) from exc

    build_starvell_package(
        lot,
        is_smm=is_smm,
        service_id=service_id,
        auto_delivery=is_smm,
    )
    desc = compose_starvell_descriptions(
        lot,
        is_smm=is_smm,
        service_id=service_id,
        min_qty=min_qty,
        max_qty=max_qty,
        include_auto_delivery=is_smm,
    )
    brief_ru = _truncate(desc["brief_ru"] or lot.title, BRIEF_MAX)
    full_ru = desc["full_ru"] or ""
    full_en = desc["full_en"] or ""

    full_description = build_bilingual_full(full_ru, full_en)
    if DEFAULT_EXECUTION_TIME not in full_description:
        full_description = f"{full_description.rstrip()}\n\n{DEFAULT_EXECUTION_TIME}".strip()
        full_description = _truncate(full_description, FULL_MAX)

    if not game_slug or not category_slug:
        game_slug, category_slug = await resolve_slugs_for_category(category_id, game_id)

    catalog = await fetch_category_catalog(game_slug, category_slug) if game_slug and category_slug else {}
    offer_type = str(catalog.get("offerType") or "LOT")
    brief_enabled = bool(catalog.get("isBriefDescriptionEnabled", True))

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

    basic_attributes, _numeric_attributes = build_offer_attributes(
        catalog,
        subcategory,
        hint_text=_hint_blob(lot),
    )

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

    availability = _resolve_availability(max_qty)

    payload: dict[str, Any] = {
        "type": offer_type,
        "categoryId": int(category_id),
        "price": format_starvell_api_price(price),
        "availability": availability,
        "isActive": True,
        "autoDelivery": False,
        "instantDelivery": False,
        "deliveryTime": DEFAULT_DELIVERY_TIME,
        "descriptions": descriptions,
    }
    if is_smm and min_qty and subcategory_supports_min_order(catalog, subcategory):
        payload["minOrderCurrencyAmount"] = int(min_qty)
    if resolved_game_id:
        payload["gameId"] = resolved_game_id
    if basic_attributes:
        payload["basicAttributes"] = basic_attributes
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
    logger.info("full payload attrs: %s sub=%s", payload_attribute_stats(payload), sub_category_id)

    payload["_catalog"] = catalog
    payload["_subcategory"] = subcategory
    payload["_brief_enabled"] = brief_enabled
    payload["_vexboost_info"] = vexboost_info
    return payload


def offer_admin_url(offer_id: str | int, public_id: str | None = None) -> str:
    oid = str(public_id or offer_id or "").strip()
    if not oid:
        return f"{BASE_URL}/"
    return f"{BASE_URL}/offers/{oid}"


def _is_attribute_error(message: str) -> bool:
    text = (message or "").lower()
    return "атрибут" in text


async def _resolve_template_attributes(
    api: StarvellAPI,
    *,
    category_id: int,
    template_offer_id: str | int | None,
) -> list[dict[str, str]] | None:
    """Атрибуты из шаблонного лота; ошибки API не блокируют create."""
    try:
        offer_id = template_offer_id
        if not offer_id:
            for cat in await api.fetch_seller_categories():
                if int(cat.get("category_id") or 0) == int(category_id) and cat.get("offer_id"):
                    offer_id = cat["offer_id"]
                    break
        if not offer_id:
            return None
        offer = await api.fetch_offer(str(offer_id))
        attrs = compact_option_attributes(offer.get("attributes"))
        return attrs or None
    except Exception as exc:
        logger.warning("template attrs skipped (cat=%s): %s", category_id, exc)
        return None


def _create_attempts(
    full_payload: dict[str, Any],
    *,
    brief_ru: str,
    game_id: int,
    category_id: int,
    catalog: dict[str, Any],
    subcategory: dict[str, Any] | None,
    brief_enabled: bool,
    template_attrs: list[dict[str, str]] | None,
) -> list[tuple[str, dict[str, Any]]]:
    """Варианты create по схеме веб-формы Starvell."""
    full_desc = _extract_full_description(full_payload)
    common = dict(
        brief_ru=brief_ru,
        full_description=full_desc,
        game_id=game_id,
        auto_delivery=False,
        catalog=catalog,
        subcategory=subcategory,
        brief_enabled=brief_enabled,
    )

    sub_id = full_payload.get("subCategoryId")
    supports_dt = subcategory_supports_delivery_time(category_id, sub_id)

    attempts: list[tuple[str, dict[str, Any]]] = [
        (
            "frontend-basic",
            build_minimal_create_payload(
                full_payload,
                include_attributes=True,
                include_delivery_time=False,
                **common,
            ),
        ),
        (
            "frontend-no-attrs",
            build_minimal_create_payload(
                full_payload,
                include_attributes=False,
                include_delivery_time=False,
                **common,
            ),
        ),
    ]

    if supports_dt:
        attempts.insert(
            1,
            (
                "frontend-with-delivery",
                build_minimal_create_payload(
                    full_payload,
                    include_attributes=True,
                    include_delivery_time=True,
                    **common,
                ),
            ),
        )

    if template_attrs:
        template_base = {
            **full_payload,
            "basicAttributes": [{"id": a["id"], "optionId": a["optionId"]} for a in template_attrs],
        }
        attempts.insert(
            0,
            (
                "frontend-template",
                build_minimal_create_payload(template_base, include_attributes=True, **common),
            ),
        )

    if int(category_id) == 175:
        builtin_base = {
            **full_payload,
            "categoryId": 175,
            "subCategoryId": int(BUILTIN_TELEGRAM_SUBSCRIBERS["sub_category_id"]),
            "basicAttributes": list(BUILTIN_TELEGRAM_SUBSCRIBERS["basic_attributes"]),
        }
        attempts.append(
            (
                "frontend-builtin",
                build_minimal_create_payload(
                    builtin_base,
                    include_attributes=True,
                    game_id=game_id or 14,
                    auto_delivery=False,
                    catalog=catalog,
                    subcategory=subcategory,
                    brief_enabled=brief_enabled,
                    brief_ru=brief_ru,
                ),
            ),
        )

    seen: set[str] = set()
    unique: list[tuple[str, dict[str, Any]]] = []
    for name, body in attempts:
        key = payload_attribute_stats(body)
        if key in seen:
            continue
        seen.add(key)
        unique.append((name, body))
    return unique


def _extract_full_description(full_payload: dict[str, Any]) -> str:
    return str(
        ((full_payload.get("descriptions") or {}).get("rus") or {}).get("description") or ""
    ).strip()


async def _apply_full_content(
    api: StarvellAPI,
    offer_id: str | int,
    public_id: str | int | None,
    full_payload: dict[str, Any],
    *,
    catalog: dict[str, Any] | None = None,
    subcategory: dict[str, Any] | None = None,
    brief_enabled: bool = True,
) -> bool:
    """Фаза 2: описание/postPayment через POST /offers/{id}/update (partial-update их не принимает)."""
    expected = _extract_full_description(full_payload)
    if not expected:
        return True

    update_body = build_offer_content_update_payload(
        full_payload,
        catalog=catalog,
        subcategory=subcategory,
        brief_enabled=brief_enabled,
    )
    if not update_body:
        return False

    ids_to_try = []
    for candidate in (public_id, offer_id):
        text = str(candidate or "").strip()
        if text and text not in ids_to_try:
            ids_to_try.append(text)

    errors: list[str] = []
    for oid in ids_to_try:
        try:
            await api.update_offer(oid, update_body)
            logger.info("update_offer content applied for %s", oid[:16])
            offer = await api.fetch_offer(oid)
            got = str(
                ((offer.get("descriptions") or {}).get("rus") or {}).get("description") or ""
            )
            marker = "ID:" if "ID:" in expected else expected[:40]
            if marker and marker in got:
                return True
            partial = build_offer_partial_update_payload(full_payload)
            if partial:
                await api.partial_update_offer(oid, partial)
            if marker in got or len(got) >= len(expected) * 0.5:
                return True
            errors.append(f"{oid}: описание не совпало ({len(got)} vs {len(expected)} chars)")
        except StarvellAPIError as exc:
            errors.append(f"{oid}: {exc}")
            logger.warning("update_offer content failed for %s: %s", oid, exc)

    if errors:
        logger.error("post-create content update failed: %s", "; ".join(errors[-3:]))
    return not errors


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
    vexboost_info: VexBoostServiceInfo | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Создаёт лот на Starvell (frontend create → partial-update)."""
    _ = auto_delivery

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
        vexboost_info=vexboost_info,
        db=db,
    )

    catalog = full_payload.pop("_catalog", {}) or {}
    subcategory = full_payload.pop("_subcategory", None)
    brief_enabled = bool(full_payload.pop("_brief_enabled", True))
    vexboost_info = full_payload.pop("_vexboost_info", None) or vexboost_info

    brief_ru = (
        ((full_payload.get("descriptions") or {}).get("rus") or {}).get("briefDescription")
        or lot.title
        or "Лот"
    )
    resolved_game_id = int(game_id or full_payload.get("gameId") or 0)
    sub_category_id = full_payload.get("subCategoryId")

    draft_note = ""
    try:
        draft = await api.fetch_offer_draft(
            category_id,
            sub_category_id=int(sub_category_id) if sub_category_id else None,
            game_slug=game_slug,
            category_slug=category_slug,
        )
        draft_count = draft_attribute_count(draft)
        if draft_count > 0:
            draft_note = f"draft_attrs={draft_count}"
            logger.info("offer draft attrs=%s cat=%s sub=%s", draft_count, category_id, sub_category_id)
    except Exception as exc:
        logger.debug("fetch_offer_draft: %s", exc)

    template_attrs = await _resolve_template_attributes(
        api,
        category_id=category_id,
        template_offer_id=template_offer_id,
    )

    async def _send(body: dict[str, Any]) -> dict[str, Any]:
        return await api.create_offer(
            body,
            category_id=category_id,
            game_slug=game_slug,
            category_slug=category_slug,
            finalize_mode="frontend",
        )

    attempts = _create_attempts(
        full_payload,
        brief_ru=str(brief_ru),
        game_id=resolved_game_id,
        category_id=category_id,
        catalog=catalog,
        subcategory=subcategory,
        brief_enabled=brief_enabled,
        template_attrs=template_attrs,
    )

    errors: list[str] = []
    result: dict[str, Any] | None = None
    winning_body: dict[str, Any] = attempts[0][1]
    winning_name = attempts[0][0]

    for name, body in attempts:
        stats = payload_attribute_stats(body)
        try:
            result = await _send(body)
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
        extra = f"\n{draft_note}" if draft_note else ""
        raise StarvellAPIError(
            400,
            (
                f"Starvell отклонил все варианты payload ({PARSER_BUILD}).{extra}\n\n"
                f"📊 Последняя попытка: {last_stats}\n\n"
                + "\n".join(errors[-5:])
            ),
            {},
        )

    offer_id = result.get("id") or result.get("offerId")
    public_id = result.get("publicId")
    content_ok = True
    if offer_id:
        content_ok = await _apply_full_content(
            api,
            offer_id,
            public_id,
            full_payload,
            catalog=catalog,
            subcategory=subcategory,
            brief_enabled=brief_enabled,
        )

    return {
        "offer_id": offer_id,
        "public_id": public_id,
        "url": offer_admin_url(offer_id or "", public_id),
        "payload": winning_body,
        "raw": result,
        "vexboost_info": vexboost_info,
        "content_applied": content_ok,
    }


def format_created_message(
    *,
    title: str,
    url: str,
    price: str,
    category_id: int,
    is_smm: bool,
    service_id: int | None,
    min_qty: int | None = None,
    max_qty: int | None = None,
) -> str:
    lines = [
        "✅ <b>Лот создан на Starvell</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📌 {title}",
        f"💰 Цена: <code>{format_price_display(price)}</code> ₽",
        f"📦 Наличие: <code>{max_qty or AVAILABILITY_LOT}</code>",
        f"📁 Категория: <code>{category_id}</code>",
    ]
    if is_smm and service_id:
        lines.append(f"🆔 VexBoost ID: <code>{service_id}</code>")
    if is_smm and min_qty and max_qty:
        lines.append(f"📦 Кол-во: <code>{min_qty}</code> — <code>{max_qty}</code> шт.")
    lines += [
        "🤖 SMM-бот: сообщение после оплаты настроено",
        "🤖 Авто-доставка: описание + postPaymentMessage",
        "🇷🇺 + 🇬🇧 Описание: RU и EN в одном поле",
        "",
        f'🔗 <a href="{url}">Открыть лот на Starvell</a>',
    ]
    return "\n".join(lines)
