"""Shared reply-keyboard labels for global navigation."""

from __future__ import annotations

REPLY_HOME = frozenset({"🏠 Меню", "Меню"})
REPLY_ACCOUNTS = frozenset({"🚀 Аккаунты", "Аккаунты"})
REPLY_ADD = frozenset({"➕ Добавить", "Добавить"})
REPLY_PROXIES = frozenset({"🌐 Прокси", "Прокси"})
REPLY_GEMINI = frozenset({"🤖 Gemini", "Gemini"})
REPLY_STATS = frozenset({"📊 Статистика", "Статистика"})
REPLY_PROFILE = frozenset({"👤 Профиль", "Профиль"})
REPLY_HELP = frozenset({"📖 Справка", "Справка"})
REPLY_ADMIN = frozenset({"🔑 Admin", "Admin"})

# Any of these must never be swallowed by FSM catch-all handlers
ALL_REPLY_NAV = (
    REPLY_HOME
    | REPLY_ACCOUNTS
    | REPLY_ADD
    | REPLY_PROXIES
    | REPLY_GEMINI
    | REPLY_STATS
    | REPLY_PROFILE
    | REPLY_HELP
    | REPLY_ADMIN
)
