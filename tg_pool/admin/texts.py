"""HTML message templates for the control panel."""

from __future__ import annotations

from html import escape
from typing import Optional, Sequence

from tg_pool.db.models import (
    Account,
    AccountStatus,
    AutoReplySettings,
    InviteCode,
    PanelUser,
    PendingDraft,
    Proxy,
)


STATUS_LABEL = {
    AccountStatus.active: "🟢 active",
    AccountStatus.flood_wait: "🟡 flood_wait",
    AccountStatus.banned: "🔴 banned",
    AccountStatus.paused: "⏸ paused",
    AccountStatus.spambot: "🚫 spambot",
}


def h(text: object) -> str:
    return escape(str(text), quote=False)


def welcome_locked(username: Optional[str]) -> str:
    name = f"@{h(username)}" if username else "друг"
    return (
        f"👋 Привет, {name}!\n\n"
        f"<b>Доступ к панели ограничен</b>\n"
        f"<blockquote>Эта система управляет пулом юзерботов и доступна "
        f"только по инвайт-коду.</blockquote>\n\n"
        f"🔑 Отправьте ключ доступа в формате:\n"
        f"<code>ALT-XXXX-YYYY</code>"
    )


def welcome_approved(user: PanelUser) -> str:
    role = h(user.role.value)
    uname = f"@{h(user.username)}" if user.username else f"<code>{user.telegram_id}</code>"
    return (
        f"✨ <b>С возвращением</b>, {uname}!\n\n"
        f"Роль: <code>{role}</code>\n"
        f"Статус: <b>доступ открыт</b>\n\n"
        f"Откройте главное меню командой /menu"
    )


def main_menu_text() -> str:
    return (
        "🏠 <b>Главное меню</b>\n\n"
        "<blockquote>Управление аккаунтами, прокси, Gemini-черновиками "
        "и доступом к панели.</blockquote>\n\n"
        "Выберите раздел:"
    )


def help_text() -> str:
    return (
        "📖 <b>Справка</b>\n\n"
        "<b>Команды</b>\n"
        "• /start — запуск / перезапуск\n"
        "• /menu — главное меню\n"
        "• /profile — ваш профиль и доступ\n"
        "• /admin — панель суперадмина\n"
        "• /help — эта справка\n"
        "• /import_tdata — импорт TData ZIP\n\n"
        "<b>Как добавить аккаунт</b>\n"
        "1) Добавьте SOCKS5/HTTP прокси\n"
        "2) Импортируйте TData ZIP или StringSession\n"
        "3) Следите за статусом FloodWait / SpamBot\n\n"
        "<b>Gemini Draft Engine</b>\n"
        "• Включите Assistant на аккаунте\n"
        "• Настройте API key в «🤖 Gemini / Черновики»\n"
        "• Одобряйте карточки: Отправить / Изменить / Отклонить\n"
        "• Авто-режим по умолчанию выключен\n\n"
        "<i>Сессии всегда работают через закреплённый прокси "
        "и сохранённый fingerprint устройства.</i>"
    )


def profile_text(user: PanelUser) -> str:
    uname = f"@{h(user.username)}" if user.username else "—"
    approved = "✅ да" if user.is_approved else "❌ нет"
    return (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Username: {uname}\n"
        f"Имя: <i>{h(user.full_name or '—')}</i>\n"
        f"Роль: <code>{h(user.role.value)}</code>\n"
        f"Доступ: <b>{approved}</b>\n"
        f"Регистрация: <code>{user.created_at}</code>"
    )


def accounts_list_text(accounts: Sequence[Account]) -> str:
    if not accounts:
        return (
            "🚀 <b>Мои аккаунты</b>\n\n"
            "<blockquote>Пока пусто. Добавьте TData или StringSession.</blockquote>"
        )
    lines = ["🚀 <b>Мои аккаунты</b>", ""]
    for acc in accounts:
        label = STATUS_LABEL.get(acc.status, acc.status.value)
        flood = f" · until <code>{acc.flood_until}</code>" if acc.flood_until else ""
        lines.append(
            f"• <b>#{acc.id}</b> <code>{h(acc.phone_number)}</code>\n"
            f"  {label}{flood} · actions/day: <code>{acc.total_actions_today}</code>"
        )
    return "\n".join(lines)


def account_detail_text(acc: Account) -> str:
    proxy_info = (
        f"{acc.proxy.protocol.value}://{acc.proxy.ip}:{acc.proxy.port}"
        if acc.proxy
        else "—"
    )
    label = STATUS_LABEL.get(acc.status, acc.status.value)
    assistant = "🤖 ON" if acc.assistant_enabled else "OFF"
    return (
        f"👤 <b>Аккаунт #{acc.id}</b>\n\n"
        f"Телефон: <code>{h(acc.phone_number)}</code>\n"
        f"Username: <code>{h(acc.display_name or '—')}</code>\n"
        f"Статус: <b>{label}</b>\n"
        f"Assistant: <b>{assistant}</b>\n"
        f"SpamBot: {'🚫 да' if acc.is_spambot_restricted else '✅ нет'}\n"
        f"Действий сегодня: <code>{acc.total_actions_today}</code>\n"
        f"Flood until: <code>{acc.flood_until or '—'}</code>\n\n"
        f"<b>Fingerprint</b>\n"
        f"<code>{h(acc.device_model)}</code>\n"
        f"<code>{h(acc.system_version)}</code> · <code>{h(acc.app_version)}</code>\n\n"
        f"Прокси: <code>{h(proxy_info)}</code>\n"
        f"Ошибка: <i>{h(acc.last_error or '—')}</i>"
    )


def drafts_settings_text(
    cfg: AutoReplySettings,
    *,
    pending_count: int,
    assistant_accounts: int,
) -> str:
    key_set = "✅ задан" if (cfg.gemini_api_key or "").strip() else "❌ нет"
    return (
        "🤖 <b>Gemini Draft Engine</b>\n\n"
        "<blockquote>Юзербот ловит триггеры → Gemini готовит черновик → "
        "оператор одобряет отправку (или auto-approve).</blockquote>\n\n"
        f"Мониторинг: <b>{'ON' if cfg.enabled else 'OFF'}</b>\n"
        f"Авто-approve: <b>{'ON' if cfg.auto_approve_enabled else 'OFF'}</b>\n"
        f"API key: {key_set}\n"
        f"Модель: <code>{h(cfg.gemini_model)}</code>\n"
        f"Продвигаем: <code>{h(cfg.promote_username)}</code>\n"
        f"Delay: <code>{cfg.delay_min_sec:.0f}–{cfg.delay_max_sec:.0f}s</code>\n"
        f"Typing: <code>{cfg.typing_min_sec:.0f}–{cfg.typing_max_sec:.0f}s</code>\n"
        f"Лимит/чат/день: <code>{cfg.max_replies_per_chat_day}</code>\n"
        f"Assistant-аккаунтов: <code>{assistant_accounts}</code>\n"
        f"Pending черновиков: <code>{pending_count}</code>"
    )


def pending_drafts_text(drafts: Sequence[PendingDraft]) -> str:
    if not drafts:
        return "📥 <b>Pending черновики</b>\n\n<blockquote>Очередь пуста.</blockquote>"
    lines = ["📥 <b>Pending черновики</b>", ""]
    for d in drafts:
        lines.append(
            f"• <b>#{d.id}</b> {h(d.chat_title or d.chat_id)} · "
            f"acc <code>#{d.account_id}</code>\n"
            f"  <i>{h((d.draft_text or '')[:80])}</i>"
        )
    return "\n".join(lines)


def proxies_text(proxies: Sequence[Proxy]) -> str:
    if not proxies:
        return "🌐 <b>Прокси</b>\n\n<blockquote>Список пуст.</blockquote>"
    lines = ["🌐 <b>Прокси</b>", ""]
    for p in proxies:
        lines.append(
            f"• <b>#{p.id}</b> <code>{p.protocol.value}://{h(p.ip)}:{p.port}</code>\n"
            f"  account={p.assigned_account_id or '—'} · "
            f"{'🟢 alive' if p.is_alive else '🔴 dead'}"
        )
    return "\n".join(lines)


def stats_text(
    *,
    total: int,
    active: int,
    flood: int,
    banned: int,
    paused: int,
    spambot: int,
    actions_today: int,
    proxies: int,
) -> str:
    return (
        "📊 <b>Статистика за сегодня</b>\n\n"
        f"<blockquote>"
        f"Аккаунтов всего: <b>{total}</b>\n"
        f"🟢 active: <b>{active}</b> · 🟡 flood: <b>{flood}</b>\n"
        f"🔴 banned: <b>{banned}</b> · ⏸ paused: <b>{paused}</b>\n"
        f"🚫 spambot: <b>{spambot}</b>\n"
        f"Прокси: <b>{proxies}</b>\n"
        f"Действий сегодня: <b>{actions_today}</b>"
        f"</blockquote>"
    )


def admin_panel_text() -> str:
    return (
        "🛡 <b>Панель суперадминистратора</b>\n\n"
        "<blockquote>Управление инвайт-кодами и доступом к системе.</blockquote>\n"
        "Выберите действие:"
    )


def invites_text(invites: Sequence[InviteCode]) -> str:
    if not invites:
        return "🔑 <b>Инвайт-коды</b>\n\n<blockquote>Пока нет кодов.</blockquote>"
    lines = ["🔑 <b>Инвайт-коды</b> <i>(последние 50)</i>", ""]
    for inv in invites:
        if inv.is_used:
            lines.append(
                f"• <code>{h(inv.code)}</code> — ✅ used by "
                f"<code>{inv.used_by}</code>"
            )
        else:
            lines.append(f"• <code>{h(inv.code)}</code> — ⏳ active")
    return "\n".join(lines)


def invite_created_text(code: str) -> str:
    return (
        "🆕 <b>Инвайт создан</b>\n\n"
        f"Код: <code>{h(code)}</code>\n"
        f"<blockquote>Передайте код пользователю. "
        f"Он одноразовый.</blockquote>"
    )


def invite_redeemed_notify(username: Optional[str], telegram_id: int, code: str) -> str:
    uname = f"@{h(username)}" if username else f"<code>{telegram_id}</code>"
    return (
        "📣 <b>Новый доступ</b>\n\n"
        f"Пользователь {uname} использовал код "
        f"<code>{h(code)}</code> и получил доступ!"
    )


def access_users_text(users: Sequence[PanelUser]) -> str:
    if not users:
        return "👥 <b>Пользователи</b>\n\nПусто."
    lines = ["👥 <b>Пользователи панели</b>", ""]
    for u in users:
        flag = "✅" if u.is_approved else "🔒"
        uname = f"@{h(u.username)}" if u.username else "—"
        lines.append(
            f"{flag} <code>{u.telegram_id}</code> {uname} · "
            f"<code>{h(u.role.value)}</code>"
        )
    return "\n".join(lines)
