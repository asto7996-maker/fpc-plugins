"""
Error classification + recovery matrix for DwarBot.

Classes
-------
AUTH          — session / OAuth; never Cursor-heal; wait for cookies
NETWORK       — transient HTTP; retry with backoff
PROTOCOL      — known game-API mismatch; local protocol recoverers
STAGNATION    — no gameplay progress; hunt/fight/quest local recover first
CODE_BUG      — real Python exceptions / corrupt logic; Cursor self-heal
RATE_LIMIT    — server soft-bans; cool down locally
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class ErrorClass(Enum):
    AUTH = auto()
    NETWORK = auto()
    PROTOCOL = auto()
    STAGNATION = auto()
    CODE_BUG = auto()
    RATE_LIMIT = auto()
    IGNORE = auto()


@dataclass
class ClassifiedError:
    kind: ErrorClass
    summary: str
    failed_file: str = "dwar_bot/main.py"
    allow_cursor: bool = False
    local_action: str = ""  # hint for local recoverer


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_AUTH_MARKERS = (
    "token expired",
    "access_token",
    "oauth",
    "soc_auth",
    "не пройдена авторизация",
    "waiting for fresh cookies",
    "нет access_token",
    "no access_token",
    "auth_blocked",
    "сессия невалид",
)

_NETWORK_MARKERS = (
    "timeout",
    "timed out",
    "connecterror",
    "connection reset",
    "temporarily unavailable",
    "503",
    "502",
    "504",
    "network is unreachable",
    "name or service not known",
    "ssl",
    "remoteprotocolerror",
)

_RATE_MARKERS = (
    "слишком часто",
    "подождите",
    "too many requests",
    "rate limit",
    "flood",
    "бан",
    "заблок",
)

_PROTOCOL_MARKERS = (
    "attack_bot",
    "напад",
    "type=2",
    "npc|answer",
    "fight_id",
    "hunt_farm",
    "крэтс",
    "военачальник",
    "покинуть селение",
    "этап не может",
    "действие не найдено",
    "status=2",
    "redirect_error",
)

_IGNORE_MARKERS = (
    "cancelled",
    "keyboardinterrupt",
)


def classify_text(text: str, *, focus_key: str = "", where: str = "") -> ClassifiedError:
    blob = f"{where}\n{focus_key}\n{text or ''}".lower()

    if any(m in blob for m in _IGNORE_MARKERS):
        return ClassifiedError(ErrorClass.IGNORE, "ignored", allow_cursor=False)

    if any(m in blob for m in _AUTH_MARKERS):
        return ClassifiedError(
            ErrorClass.AUTH,
            "auth/session",
            failed_file="dwar_bot/core/game_client.py",
            allow_cursor=False,
            local_action="wait_cookies",
        )

    if any(m in blob for m in _NETWORK_MARKERS):
        return ClassifiedError(
            ErrorClass.NETWORK,
            "network/transient",
            allow_cursor=False,
            local_action="retry_backoff",
        )

    if any(m in blob for m in _RATE_MARKERS):
        return ClassifiedError(
            ErrorClass.RATE_LIMIT,
            "rate/cooldown",
            allow_cursor=False,
            local_action="cooldown",
        )

    if "stagnation" in blob or "dom-desync" in blob:
        return ClassifiedError(
            ErrorClass.STAGNATION,
            f"stagnation:{focus_key or '?'}",
            failed_file=_file_for_focus(focus_key),
            allow_cursor=True,  # only after local recover fails
            local_action="gameplay_recover",
        )

    if any(m in blob for m in _PROTOCOL_MARKERS):
        return ClassifiedError(
            ErrorClass.PROTOCOL,
            "game protocol",
            failed_file=_file_for_focus(focus_key) or "dwar_bot/modules/quest_tracker.py",
            allow_cursor=True,
            local_action="gameplay_recover",
        )

    # Default: real code bug
    return ClassifiedError(
        ErrorClass.CODE_BUG,
        "code exception",
        failed_file=_guess_file(text) or "dwar_bot/main.py",
        allow_cursor=True,
        local_action="",
    )


def classify_exception(exc: BaseException, *, where: str = "") -> ClassifiedError:
    name = type(exc).__name__
    msg = str(exc)
    tb = ""
    try:
        import traceback
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        pass
    if name in ("TokenExpiredError", "AuthRequiredError"):
        return ClassifiedError(
            ErrorClass.AUTH, msg[:200],
            failed_file="dwar_bot/core/game_client.py",
            allow_cursor=False,
            local_action="wait_cookies",
        )
    if name in ("TimeoutException", "ConnectError", "ReadTimeout", "ConnectTimeout"):
        return ClassifiedError(
            ErrorClass.NETWORK, f"{name}: {msg[:160]}",
            allow_cursor=False,
            local_action="retry_backoff",
        )
    return classify_text(f"{name}: {msg}\n{tb}", where=where)


def _guess_file(tb: str) -> str:
    paths = re.findall(r'File "([^"]+\.py)"', tb or "")
    for p in reversed(paths):
        norm = p.replace("\\", "/")
        if "dwar_bot/" in norm:
            return norm[norm.find("dwar_bot/"):]
    return paths[-1] if paths else "dwar_bot/main.py"


def _file_for_focus(focus_key: str) -> str:
    low = (focus_key or "").lower()
    if "hunt" in low or "охот" in low or "крэт" in low:
        return "dwar_bot/modules/combat_engine.py"
    if "quest" in low or "npc" in low or "вожд" in low:
        return "dwar_bot/modules/quest_tracker.py"
    if "fight" in low or "схватк" in low or "бой" in low:
        return "dwar_bot/modules/fight_client.py"
    if "combat" in low or "расселин" in low or "front" in low or "arena" in low:
        return "dwar_bot/modules/progression_brain.py"
    if "travel" in low or "переход" in low:
        return "dwar_bot/main.py"
    return "dwar_bot/main.py"


def cursor_prompt_for(classified: ClassifiedError, raw: str) -> str:
    """Build a game-logic-aware Cursor heal prompt."""
    base = raw[-3500:] if raw else classified.summary
    hints = {
        ErrorClass.STAGNATION: (
            "Это геймплейный застой, НЕ DOM. Логика игры dwar.ru:\n"
            "1) Квест «Проба сил» type=2 требует УБИТЬ живого моба с hunt_farm "
            "(ATTACK_BOT live id), затем npc|answer.\n"
            "2) fight_id берётся из state.fight_id; бой завершается через wsproxy.\n"
            "3) Не спамить фронты/Расселину в деревне новичка — prefer HUNT_MOB.\n"
            "4) Не инвалидировать sess_sid при пустом nick — soft retry.\n"
            "Исправь протокол в боевых/квестовых модулях минимальным диффом."
        ),
        ErrorClass.PROTOCOL: (
            "Ошибка игрового HTTP/WS протокола dwar.ru. "
            "Проверь hunt_farm XML, ATTACK_BOT GET in[], fight WS INIT/ATTACK, "
            "npc|answer type=2. Минимальный дифф, без рефакторинга."
        ),
        ErrorClass.CODE_BUG: (
            "Обычный Python exception. Найди корневую причину по traceback, "
            "минимальный фикс. Не трогай .env / секреты."
        ),
    }
    extra = hints.get(classified.kind, hints[ErrorClass.CODE_BUG])
    return (
        f"[{classified.kind.name}] {classified.summary}\n\n"
        f"{extra}\n\n---\n{base}"
    )


@dataclass
class RecoveryStats:
    auth_waits: int = 0
    network_retries: int = 0
    protocol_recovers: int = 0
    stagnation_local: int = 0
    cursor_heals: int = 0
    ignored: int = 0
    last_kind: str = ""
    last_at: float = 0.0
    history: list[str] = field(default_factory=list)

    def note(self, kind: ErrorClass, detail: str = "") -> None:
        self.last_kind = kind.name
        self.last_at = time.time()
        line = f"{time.strftime('%H:%M:%S')} {kind.name} {detail}"[:120]
        self.history.append(line)
        self.history = self.history[-40:]
        if kind == ErrorClass.AUTH:
            self.auth_waits += 1
        elif kind == ErrorClass.NETWORK:
            self.network_retries += 1
        elif kind == ErrorClass.PROTOCOL:
            self.protocol_recovers += 1
        elif kind == ErrorClass.STAGNATION:
            self.stagnation_local += 1
        elif kind == ErrorClass.CODE_BUG:
            self.cursor_heals += 1
        elif kind == ErrorClass.IGNORE:
            self.ignored += 1


_STATS = RecoveryStats()


def get_recovery_stats() -> RecoveryStats:
    return _STATS
