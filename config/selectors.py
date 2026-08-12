"""
CSS / XPath selectors for DwarBot (browser DOM fallbacks).

The live HTTP bot primarily uses the JSON API; these selectors are kept for
Playwright paths and for the Cursor self-healer prompt to cross-check.
Canonical definitions live in ``dwar_bot.config.SELECTORS``.
"""

from __future__ import annotations

try:
    from dwar_bot.config import SELECTORS as _S
except Exception:  # pragma: no cover — allow import without package on PATH
    _S = None


def _get(name: str, default: str) -> str:
    if _S is None:
        return default
    return getattr(_S, name, default)


# --- Login ---
LOGIN_USERNAME = _get("login_username", "#login-form input[name='login']")
LOGIN_PASSWORD = _get("login_password", "#login-form input[name='password']")
LOGIN_SUBMIT = _get("login_submit", "#login-form button[type='submit']")

# --- Character ---
CHAR_NAME = _get("char_name", ".char-name, #char-name")
CHAR_LEVEL = _get("char_level", ".char-level, #char-level")
CHAR_HP_CURRENT = _get("char_hp_current", ".hp-current, #hp-current")
CHAR_HP_MAX = _get("char_hp_max", ".hp-max, #hp-max")

# --- Combat ---
COMBAT_ATTACK = _get("combat_attack_btn", ".combat-attack, #btn-attack")
COMBAT_LOG = _get("combat_log", ".combat-log, #battle-log")

# --- Quests / NPC ---
NPC_DIALOGUE = _get("npc_dialogue_box", ".dialogue-box, #npc-dialog")
NPC_CHOICES = _get("npc_choice_btns", ".dialogue-choice, .npc-choice")
