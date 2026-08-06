#!/usr/bin/env python3
"""
Диагностика кнопки поддержки и маршрутизации меню VK-бота.

Запуск из папки vk_vpn_bot:
    .venv/bin/python tools/diagnose_support.py

Проверяет:
  1. SUPPORT_URL — схему, домен, «зацикленность» на диалог с самим ботом;
  2. клавиатуры — лимиты VK (строки, кнопки, длина подписи, тип действия);
  3. маршрутизацию — каждая подпись кнопки должна попадать в свой обработчик;
  4. настройки сообщества через VK API (включены ли сообщения).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keyboards.menus as M
from config import get_settings

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"

problems: list[str] = []


def report(level: str, title: str, detail: str = "") -> None:
    print(f"[{level}] {title}")
    if detail:
        for line in detail.splitlines():
            print(f"       {line}")
    if level in {WARN, FAIL}:
        problems.append(f"{level}: {title}")


def check_support_url(settings) -> None:
    print("\n=== 1. SUPPORT_URL ===")
    from services.support import is_self_dialog_url, usable_support_url

    url = (settings.support_url or "").strip()
    print(f"значение: {url!r}")

    if not url:
        report(
            OK,
            "SUPPORT_URL не задан",
            "Внешняя кнопка не рисуется, вопрос принимается прямо в чате.",
        )
        return

    parsed = urlparse(url)
    if parsed.scheme != "https":
        report(
            WARN,
            f"схема {parsed.scheme!r} вместо https",
            "VK принимает в open_link только https — ссылка будет скрыта.",
        )
    else:
        report(OK, "схема https")

    if is_self_dialog_url(url, settings.group_id):
        report(
            WARN,
            "ссылка ведёт в диалог с этим же сообществом",
            f"GROUP_ID={abs(int(settings.group_id))}.\n"
            "Пользователь уже в этом чате, поэтому кнопка-ссылка скрывается,\n"
            "а вопрос принимается через «✍️ Задать вопрос».",
        )
    else:
        report(OK, "ссылка не зациклена на текущий диалог")

    print(f"итоговая ссылка для кнопки: {usable_support_url(settings)!r}")


def check_keyboards(settings) -> None:
    print("\n=== 2. Клавиатуры и лимиты VK ===")
    from services.support import usable_support_url

    variants = {
        "main_menu": M.main_menu_keyboard(settings.cabinet_url),
        "support": M.support_keyboard(
            usable_support_url(settings), settings.cabinet_url
        ),
        "support_wait": M.support_wait_keyboard(),
        "info": M.info_keyboard(settings.cabinet_url),
        "connect": M.connect_keyboard(
            has_active=False, trial_available=True, cabinet_url=settings.cabinet_url
        ),
        "pay_methods": M.pay_methods_keyboard(),
        "pay_amounts": M.pay_amounts_keyboard(),
        "subscription": M.subscription_keyboard(settings.cabinet_url, has_key=True),
        "doc_nav": M.doc_nav_keyboard(page=1, total=2),
    }

    for name, raw in variants.items():
        data = json.loads(raw)
        rows = data.get("buttons", [])
        total = sum(len(r) for r in rows)
        max_row = max((len(r) for r in rows), default=0)
        long_labels = [
            b["action"].get("label", "")
            for r in rows
            for b in r
            if len(b["action"].get("label", "")) > 40
        ]
        inline = data.get("inline", False)
        limit_rows = 6 if inline else 10
        issues = []
        if len(rows) > limit_rows:
            issues.append(f"строк {len(rows)} > {limit_rows}")
        if max_row > 5:
            issues.append(f"кнопок в строке {max_row} > 5")
        if total > 40:
            issues.append(f"всего кнопок {total} > 40")
        if long_labels:
            issues.append(f"длинные подписи: {long_labels}")

        status = OK if not issues else FAIL
        report(
            status,
            f"{name}: строк={len(rows)} кнопок={total} inline={inline}",
            "; ".join(issues),
        )


def collect_button_labels() -> list[str]:
    return [
        v
        for k, v in vars(M).items()
        if k.startswith("BTN_") and isinstance(v, str)
    ]


def _message_handlers() -> list[tuple[str, list]]:
    """Обработчики сообщений в порядке регистрации: (имя функции, правила)."""
    from handlers.start import labeler

    result: list[tuple[str, list]] = []
    for view in labeler.views().values():
        handlers = getattr(view, "handlers", None)
        if not isinstance(handlers, list):
            continue  # raw_event view хранит обработчики иначе
        for handler in handlers:
            fn = getattr(handler, "handler", None)
            result.append(
                (getattr(fn, "__name__", "?"), list(getattr(handler, "rules", [])))
            )
    return result


def _match(text: str, rules: list) -> bool:
    """
    Совпадает ли текст с текстовыми правилами обработчика.

    Критерий совпадения повторяет VBMLRule.check из vkbottle:
    результат patcher.check считается успехом, только если это не None и не False.
    """
    saw_text_rule = False
    for rule in rules:
        patterns = getattr(rule, "patterns", None)
        patcher = getattr(rule, "patcher", None)
        if patterns is None or patcher is None:
            continue  # PeerRule и прочие — не про текст
        saw_text_rule = True
        hit = any(
            patcher.check(p, text) not in (None, False) for p in patterns
        )
        if not hit:
            return False
    return saw_text_rule


def resolve(text: str) -> str:
    """Какой обработчик реально получит это сообщение."""
    for name, rules in _message_handlers():
        if _match(text, rules):
            return name
        if not any(getattr(r, "patterns", None) for r in rules):
            return name  # обработчик без текстового фильтра — fallback
    return "—"


def check_routing() -> None:
    print("\n=== 3. Маршрутизация подписей кнопок ===")

    labels = collect_button_labels()
    expected = {
        M.BTN_SUPPORT: "cmd_support",
        M.BTN_INFO: "cmd_info",
        M.BTN_PRIVACY: "cmd_document",
        M.BTN_TERMS: "cmd_document",
        M.BTN_OFFER: "cmd_document",
        M.BTN_RULES: "cmd_document",
        M.BTN_FAQ: "cmd_document",
        M.BTN_CONNECT: "cmd_connect",
        M.BTN_SUBSCRIPTION: "cmd_subscription",
        M.BTN_PAY: "cmd_pay",
        M.BTN_BUY: "cmd_buy",
        M.BTN_BALANCE: "cmd_balance",
        M.BTN_CABINET: "cmd_cabinet_login",
        M.BTN_APPS: "cmd_apps",
        M.BTN_GUIDE: "cmd_guide",
        M.BTN_PROMO: "cmd_promo",
        M.BTN_REFERRAL: "cmd_referral",
        M.BTN_TRIAL: "cmd_get_key",
        M.BTN_MY_KEY: "cmd_get_key",
        M.BTN_RENEW: "cmd_renew",
        M.BTN_BACK: "cmd_start",
        M.BTN_DOC_NEXT: "cmd_doc_next",
        M.BTN_DOC_LIST: "cmd_doc_list",
        M.BTN_PAY_SBP: "cmd_pay_sbp",
        M.BTN_PAY_CARD: "cmd_pay_card",
        M.BTN_PAY_CRYPTO: "cmd_pay_crypto",
        M.BTN_ASK: "cmd_ask_question",
    }
    # Подпись объявлена, но ни в одну клавиатуру не попадает — обработчик не нужен.
    unused_labels = {M.BTN_DEVICES}

    fell_through = []
    mismatched = []
    for label in labels:
        target = resolve(label)
        if target in {"fallback", "—"} and label not in unused_labels:
            fell_through.append(f"{label} -> {target}")
        want = expected.get(label)
        if want and target != want:
            mismatched.append(f"{label} -> {target} (ожидался {want})")

    print(f"проверено подписей: {len(labels)}")
    if fell_through:
        report(FAIL, "кнопки уходят в fallback", "\n".join(fell_through))
    else:
        report(OK, "ни одна кнопка не уходит в fallback")

    if mismatched:
        report(FAIL, "кнопка попадает не в свой обработчик", "\n".join(mismatched))
    else:
        report(OK, "все кнопки попадают в ожидаемые обработчики")

    for label, want in ((M.BTN_SUPPORT, "cmd_support"), (M.BTN_ASK, "cmd_ask_question")):
        target = resolve(label)
        report(OK if target == want else FAIL, f"«{label}» -> {target}")

    # Кнопки клавиатур должны быть реально нажимаемыми: проверяем то,
    # что VK отрисует, а не только объявленные константы.
    from services.support import usable_support_url

    rendered = set()
    for raw in (
        M.main_menu_keyboard(""),
        M.support_keyboard(usable_support_url(get_settings()), ""),
        M.info_keyboard(""),
        M.support_wait_keyboard(),
    ):
        for row in json.loads(raw)["buttons"]:
            for btn in row:
                if btn["action"]["type"] == "text":
                    rendered.add(btn["action"]["label"])
    dead = [lbl for lbl in sorted(rendered) if resolve(lbl) in {"fallback", "—"}]
    report(
        OK if not dead else FAIL,
        "все отрисованные текстовые кнопки обрабатываются",
        "\n".join(dead),
    )

    order = [name for name, _ in _message_handlers()]
    if "fallback" in order and order[-1] != "fallback":
        report(
            FAIL,
            "fallback зарегистрирован не последним",
            f"хвост порядка: {order[-3:]}",
        )
    else:
        report(OK, "fallback зарегистрирован последним")


async def check_group(settings) -> None:
    print("\n=== 4. Сообщество и адресаты поддержки (VK API) ===")
    try:
        from vkbottle import API

        from services.admin import resolve_ticket_recipients

        api = API(token=settings.vk_token)
        gid = abs(int(settings.group_id))

        groups = await api.request(
            "groups.getById", {"group_id": gid, "fields": "screen_name,name"}
        )
        payload = groups.get("response", {})
        items = payload.get("groups") if isinstance(payload, dict) else payload
        info = (items or [{}])[0] if isinstance(items, list) else {}
        report(OK, f"сообщество: {info.get('name')} (@{info.get('screen_name')})")

        lp = await api.request("groups.getLongPollSettings", {"group_id": gid})
        events = lp.get("response", {}).get("events", {})
        need = {
            "message_new": events.get("message_new"),
            "message_event": events.get("message_event"),
        }
        bad = [k for k, v in need.items() if not v]
        report(
            OK if not bad else FAIL,
            f"Long Poll события: {need}",
            ("Выключены: " + ", ".join(bad)) if bad else "",
        )

        admins = await resolve_ticket_recipients(api, settings)
        if settings.main_admin_vk_id:
            source = f"MAIN_ADMIN_VK_ID (@{settings.main_admin_username})"
        elif settings.support_admin_ids:
            source = "SUPPORT_ADMIN_IDS"
        else:
            source = "groups.getMembers"
        report(
            OK if admins else WARN,
            f"адресаты уведомлений: {admins or 'не найдены'} (источник: {source})",
            ""
            if admins
            else "Вопросы всё равно останутся в диалоге сообщества,\n"
            "но push-уведомление администратору не уйдёт.",
        )

        # Уведомление уходит от имени сообщества: проверяем, что адресат
        # разрешил такие сообщения, иначе messages.send вернёт ошибку 901.
        if admins:
            allowed = await api.request(
                "messages.isMessagesFromGroupAllowed",
                {"group_id": gid, "user_id": admins[0]},
            )
            is_allowed = bool(
                allowed.get("response", {}).get("is_allowed")
            )
            report(
                OK if is_allowed else WARN,
                f"админ {admins[0]} разрешил сообщения от сообщества: {is_allowed}",
                ""
                if is_allowed
                else "Пока не разрешил — уведомления не дойдут.\n"
                "Нужно один раз написать сообществу из своего профиля.",
            )
    except Exception as exc:  # noqa: BLE001
        report(WARN, "не удалось опросить VK API", str(exc))


async def main() -> None:
    settings = get_settings()
    print("=" * 60)
    print("ДИАГНОСТИКА: кнопка поддержки и меню VK-бота")
    print("=" * 60)

    check_support_url(settings)
    check_keyboards(settings)
    check_routing()
    await check_group(settings)

    print("\n" + "=" * 60)
    if problems:
        print(f"НАЙДЕНО ПРОБЛЕМ: {len(problems)}")
        for p in problems:
            print(f"  • {p}")
    else:
        print("Проблем не найдено.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
