#!/usr/bin/env python3
"""
Диагностика UI VK-бота: клавиатуры, маршрутизация, лимиты VK.

Запуск из папки vk_vpn_bot:
    python3 tools/diagnose_support.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keyboards.menus as M
from config import get_settings

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"
MAX_REPLY_BUTTONS = 5

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
    if not url:
        report(OK, "SUPPORT_URL не задан — вопросы в чате")
        return
    if is_self_dialog_url(url, settings.group_id):
        report(WARN, "ссылка на этот же диалог — скрыта")
    else:
        report(OK, f"внешняя ссылка: {usable_support_url(settings)!r}")


def _count_reply_buttons(raw: str) -> tuple[int, int, int]:
    data = json.loads(raw)
    rows = data.get("buttons", [])
    if data.get("inline"):
        return len(rows), max((len(r) for r in rows), default=0), sum(len(r) for r in rows)
    total = sum(len(r) for r in rows)
    max_row = max((len(r) for r in rows), default=0)
    return len(rows), max_row, total


def check_keyboards(settings) -> None:
    print("\n=== 2. Клавиатуры (≤5 кнопок в строке) ===")
    from services.support import usable_support_url

    variants = {
        "main_menu": M.main_menu_keyboard(settings.cabinet_url),
        "main_admin": M.main_menu_keyboard(settings.cabinet_url, show_admin=True),
        "help": M.help_keyboard(usable_support_url(settings)),
        "pay_methods": M.pay_methods_keyboard(),
        "pay_amounts": M.pay_amounts_keyboard(),
        "connect_trial": M.connect_keyboard(
            has_active=False, trial_available=True, cabinet_url=""
        ),
        "connect_active": M.connect_keyboard(
            has_active=True, trial_available=False, cabinet_url=""
        ),
        "subscription": M.subscription_keyboard("", has_key=True),
        "admin": M.admin_keyboard(),
        "back": M.back_keyboard(),
    }

    for name, raw in variants.items():
        rows, max_row, total = _count_reply_buttons(raw)
        issues = []
        if max_row > MAX_REPLY_BUTTONS:
            issues.append(f"в строке {max_row} > {MAX_REPLY_BUTTONS}")
        if rows > 10:
            issues.append(f"строк {rows} > 10")
        report(
            OK if not issues else FAIL,
            f"{name}: строк={rows} макс/строка={max_row} всего={total}",
            "; ".join(issues),
        )


def collect_button_labels() -> list[str]:
    return [
        v for k, v in vars(M).items() if k.startswith("BTN_") and isinstance(v, str)
    ]


def _message_handlers() -> list[tuple[str, list]]:
    from handlers.start import labeler

    result: list[tuple[str, list]] = []
    for view in labeler.views().values():
        handlers = getattr(view, "handlers", None)
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            fn = getattr(handler, "handler", None)
            result.append(
                (getattr(fn, "__name__", "?"), list(getattr(handler, "rules", [])))
            )
    return result


def _match(text: str, rules: list) -> bool:
    saw_text_rule = False
    for rule in rules:
        patterns = getattr(rule, "patterns", None)
        patcher = getattr(rule, "patcher", None)
        if patterns is None or patcher is None:
            continue
        saw_text_rule = True
        if not any(patcher.check(p, text) not in (None, False) for p in patterns):
            return False
    return saw_text_rule


def resolve(text: str) -> str:
    for name, rules in _message_handlers():
        if _match(text, rules):
            return name
        if not any(getattr(r, "patterns", None) for r in rules):
            return name
    return "—"


def check_routing() -> None:
    print("\n=== 3. Маршрутизация ===")
    expected = {
        M.BTN_HELP: "cmd_help",
        M.BTN_SUPPORT: "cmd_help",
        M.BTN_INFO: "cmd_info",
        M.BTN_CONNECT: "cmd_connect",
        M.BTN_SUBSCRIPTION: "cmd_subscription",
        M.BTN_PAY: "cmd_pay",
        M.BTN_TARIFFS: "cmd_buy",
        M.BTN_BUY: "cmd_buy",
        M.BTN_CABINET: "cmd_cabinet_login",
        M.BTN_GUIDE: "cmd_guide",
        M.BTN_PROMO: "cmd_promo",
        M.BTN_TRIAL: "cmd_get_key",
        M.BTN_MY_KEY: "cmd_get_key",
        M.BTN_RENEW: "cmd_renew",
        M.BTN_BACK: "cmd_start",
        M.BTN_PAY_SBP: "cmd_pay_sbp",
        M.BTN_PAY_CARD: "cmd_pay_card",
        M.BTN_PAY_CRYPTO: "cmd_pay_crypto",
        M.BTN_ASK: "cmd_ask_question",
        M.BTN_ADMIN: "cmd_admin",
    }

    mismatched = []
    for label, want in expected.items():
        got = resolve(label)
        if got != want:
            mismatched.append(f"{label} -> {got} (ожидался {want})")

    if mismatched:
        report(FAIL, "неверная маршрутизация", "\n".join(mismatched))
    else:
        report(OK, f"ключевые кнопки ({len(expected)}) маршрутизируются верно")

    from services.support import usable_support_url

    rendered: set[str] = set()
    for raw in (
        M.main_menu_keyboard(""),
        M.help_keyboard(usable_support_url(get_settings())),
        M.pay_methods_keyboard(),
    ):
        for row in json.loads(raw)["buttons"]:
            for btn in row:
                if btn["action"]["type"] == "text":
                    rendered.add(btn["action"]["label"])
    dead = [lbl for lbl in sorted(rendered) if resolve(lbl) in {"fallback", "—"}]
    report(
        OK if not dead else FAIL,
        "отрисованные reply-кнопки обрабатываются",
        "\n".join(dead),
    )


async def check_group(settings) -> None:
    print("\n=== 4. VK API ===")
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
        report(OK, f"сообщество: {info.get('name')}")

        admins = await resolve_ticket_recipients(api, settings)
        report(OK if admins else WARN, f"тикеты → {admins or 'никому'}")
    except Exception as exc:  # noqa: BLE001
        report(WARN, "VK API недоступен", str(exc))


async def main() -> None:
    settings = get_settings()
    print("=" * 60)
    print("ДИАГНОСТИКА UI VK-БОТА")
    print("=" * 60)
    check_support_url(settings)
    check_keyboards(settings)
    check_routing()
    await check_group(settings)
    print("\n" + "=" * 60)
    if problems:
        print(f"ПРОБЛЕМ: {len(problems)}")
        for p in problems:
            print(f"  • {p}")
    else:
        print("Проблем не найдено.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
