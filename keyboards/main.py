"""Premium главное меню."""

from __future__ import annotations


def premium_main_menu():
    from tg_bot import keyboards as KB
    return KB.main_menu()


def premium_main_text(version: str) -> str:
    from config import load_settings
    s = load_settings()
    starvell = "🟢" if s.is_starvell_configured() else "🔴"
    gemini = "🟢" if s.is_gemini_configured() else "🔴"
    user = s.starvell_username or "не привязан"
    return (
        f"✨ <b>Starvell Cardinal</b> v{version}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <code>{user}</code>\n"
        f"Starvell {starvell}  ·  Gemini {gemini}\n\n"
        f"<i>Панель управления магазином · всё в одном месте</i>"
    )
