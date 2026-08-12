"""
GeminiAuditor — Level-1 health analyst (Gemini API).

Every orchestrator tick sends a log slice + telemetry summary and expects
a strict JSON verdict for Cursor (Level-2).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]

_ISSUE_TYPES = frozenset({"CRASH", "STUCK_NO_PROGRESS", "DOM_CHANGED"})

_DEFAULT_MODEL = "gemini-flash-latest"
_MODEL_FALLBACKS = (
    "gemini-flash-latest",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)


def _preferred_model(explicit: str = "") -> str:
    _load_dotenv()
    return (explicit or os.environ.get("GEMINI_MODEL") or _DEFAULT_MODEL).strip()


def _load_dotenv() -> None:
    try:
        from dwar_bot.core.cursor_self_healer import _load_dotenv as _shared

        _shared(overwrite=False)
        return
    except Exception:
        pass
    for target in (
        REPO_ROOT / ".env",
        REPO_ROOT / "dwar_bot" / ".env",
        Path("/root/dwar_bot/.env"),
        Path("/root/.env"),
    ):
        if not target.exists():
            continue
        try:
            for raw in target.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key and not str(os.environ.get(key, "")).strip():
                    os.environ[key] = value
        except OSError:
            continue


def _gemini_api_key() -> str:
    _load_dotenv()
    # New Auth keys (AQ.) and legacy Standard keys (AIza) both use these env vars.
    # google-genai prefers GOOGLE_API_KEY when both are set — keep them in sync.
    key = (
        (os.environ.get("GEMINI_API_KEY") or "").strip()
        or (os.environ.get("GOOGLE_API_KEY") or "").strip()
    )
    if key:
        os.environ["GEMINI_API_KEY"] = key
        # Mirror so Client() auto-detect and either env name work for AQ. auth keys
        if not (os.environ.get("GOOGLE_API_KEY") or "").strip():
            os.environ["GOOGLE_API_KEY"] = key
    return key


def _key_kind(key: str) -> str:
    if key.startswith("AQ."):
        return "auth_key_AQ"
    if key.startswith("AIza"):
        return "standard_AIza"
    return "unknown"


_PROMPT_TEMPLATE = """Ты — Главный Архитектор бота для 'Легенды: Наследие Драконов'. Проанализируй состояние бота за последние 120 секунд.

ВХОДНЫЕ ДАННЫЕ:
- Текущий статус: {current_state}
- Лог событий: {log_slice}
- Телеметрия (прогресс Exp/Gold/HP): {telemetry_summary}

КРИТЕРИИ АНАЛИЗА:
1. Есть ли явные Traceback / Python ошибки / TimeoutError / DOM-Desync?
2. Есть ли ЗАВИСАНИЕ или ОТСУТСТВИЕ ПРОГРЕССА (например, за 120 секунд опыт не вырос, персонаж стоит на месте или зациклился клик по кнопке)?

ОТВЕТ ВЕРНИ СТРОГО В JSON ФОРМАТЕ (без markdown):
{{
  "issue_detected": true/false,
  "issue_type": "CRASH" / "STUCK_NO_PROGRESS" / "DOM_CHANGED",
  "target_file": "относительный путь к файлу (например, modules/combat_engine.py)",
  "cursor_prompt": "Детальная инструкция для Cursor AI: что именно сломалось, где застрял бот и как это исправить."
}}
"""


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S | re.I)
    if fence:
        raw = fence.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_verdict(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    detected = bool(data.get("issue_detected"))
    issue_type = str(data.get("issue_type") or "STUCK_NO_PROGRESS").upper()
    if issue_type not in _ISSUE_TYPES:
        issue_type = "STUCK_NO_PROGRESS"
    target = str(data.get("target_file") or "dwar_bot/main.py").strip()
    # Prefer dwar_bot/… paths
    if target.startswith("modules/") or target.startswith("core/"):
        target = f"dwar_bot/{target}"
    prompt = str(data.get("cursor_prompt") or "").strip()
    if detected and not prompt:
        prompt = (
            f"Исправь проблему типа {issue_type} в файле {target}. "
            "Минимальный дифф, без рефакторинга и без правок .env."
        )
    return {
        "issue_detected": detected,
        "issue_type": issue_type,
        "target_file": target,
        "cursor_prompt": prompt,
    }


def _strip_healing_noise(log_slice: str) -> str:
    """Drop self-heal / Cursor / Gemini lines so they cannot fake CRASH/STUCK."""
    skip_sub = (
        "ai_healing",
        "self_healing",
        "cursor_executor",
        "cursor_engine",
        "cursor_self_healer",
        "gemini_auditor",
        "healingorchestrator",
        "autonomouslogwatcher",
        "google_genai",
        "local_fixer",
        "cursorexecutor",
        "cursor cli timed out",
        "timeout after 150s",
        "agentn.global.api",
    )
    out = []
    skip_tb = False
    for line in (log_slice or "").splitlines():
        low = line.lower()
        if any(s in low for s in skip_sub):
            skip_tb = True
            continue
        if "subprocess.timeoutexpired" in low or low.startswith("timeoutexpired"):
            skip_tb = True
            continue
        # Swallow Traceback frames that belong to a just-skipped heal ERROR
        if skip_tb:
            stripped = line.lstrip()
            if (
                low.startswith("traceback (most recent call last)")
                or stripped.startswith("File ")
                or low.startswith("subprocess.")
                or (
                    stripped
                    and not stripped[0].isdigit()
                    and (
                        stripped.endswith("Error")
                        or "Error:" in stripped
                        or stripped.startswith("During handling")
                    )
                )
                or (line.startswith(" ") or line.startswith("\t"))
            ):
                continue
            # Next dated log line → resume normal filtering
            if len(line) >= 4 and line[0].isdigit():
                skip_tb = False
            else:
                continue
        out.append(line)
    return "\n".join(out)


def _heuristic_audit(
    log_slice: str,
    telemetry_summary: dict,
    current_state: str,
) -> Optional[dict[str, Any]]:
    """Offline / no-key fallback so orchestrator still works."""
    # HEURISTIC_NO_FALSE_PAUSE_V1 — ignore heal Tracebacks; wins ≠ stuck
    clean_slice = _strip_healing_noise(log_slice)
    blob = clean_slice.lower()
    tel = telemetry_summary or {}

    # Auth / cookie waits are operational, not code crashes
    auth_only = (
        "token expired" in blob
        or "waiting for fresh cookies" in blob
        or "waiting for cookies" in blob
        or "oauth access_token" in blob
    ) and not any(
        m in blob
        for m in ("syntaxerror", "attributeerror", "typeerror", "nameerror", "importerror")
    )
    if auth_only:
        return {
            "issue_detected": False,
            "issue_type": "STUCK_NO_PROGRESS",
            "target_file": "dwar_bot/main.py",
            "cursor_prompt": "",
        }

    crash_markers = (
        "syntaxerror",
        "attributeerror",
        "typeerror",
        "keyerror",
        "nameerror",
        "importerror",
        "dom-desync",
        "dom desync",
        "selector not found",
    )
    # Real game-code crash only — not Cursor/heal TimeoutExpired Tracebacks
    has_typed_crash = any(m in blob for m in crash_markers)
    has_traceback = "traceback (most recent call last)" in blob
    heal_tb = (
        "timeoutexpired" in blob
        or "cursor cli" in blob
        or "agentn.global.api" in blob
    )
    has_real_crash = has_typed_crash or (
        has_traceback
        and not heal_tb
        and "tokenexpirederror" not in blob
        and "authrequirederror" not in blob
        and "waiting for fresh cookies" not in blob
    )
    stuck_markers = (
        "local recover for stagnation",
        "завис",
        "одни и те же",
        "npc 409 — поговорить об излечении",
        "stagnation",
    )
    # "диалог не сдвинулся" alone is normal during heal_wounded / gated travel
    soft_stuck = ("диалог не сдвинулся",)
    dom_markers = ("dom_changed", "selector not found", "iframe", "playwright timeout")

    if has_real_crash:
        target = "dwar_bot/main.py"
        if "combat_engine" in blob or "fight_client" in blob:
            target = "dwar_bot/modules/combat_engine.py"
        elif "quest_tracker" in blob or "walk_npc" in blob:
            target = "dwar_bot/modules/quest_tracker.py"
        elif "game_client" in blob or "entry_point" in blob:
            target = "dwar_bot/core/game_client.py"
        return {
            "issue_detected": True,
            "issue_type": "CRASH",
            "target_file": target,
            "cursor_prompt": (
                f"В логах за 120с обнаружен crash/traceback при state={current_state}. "
                f"Исправь корневую ошибку в {target}. Лог:\n{clean_slice[-2500:]}"
            ),
        }

    if any(m in blob for m in dom_markers) and (
        "failed" in blob or "error" in blob or "timeout" in blob
    ):
        return {
            "issue_detected": True,
            "issue_type": "DOM_CHANGED",
            "target_file": "config/selectors.py",
            "cursor_prompt": (
                "Похоже на DOM/selector drift. Сверь selectors.py с актуальным "
                f"UI и поправь хрупкие локаторы. state={current_state}. "
                f"Лог:\n{clean_slice[-2000:]}"
            ),
        }

    exp_delta = float(tel.get("exp_delta") or tel.get("exp_gained") or 0)
    gold_delta = float(tel.get("gold_delta") or 0)
    battles = int(tel.get("battles") or tel.get("wins") or 0)
    wins = int(tel.get("wins") or 0)
    same_focus = int(tel.get("same_focus_ticks") or 0)
    # Live wins / fights mean the bot is progressing — never pause for "stuck"
    if (
        battles > 0
        or wins > 0
        or "бой выигран" in blob
        or "fight: fightover" in blob
        or "telemetry battle win" in blob
    ):
        return {
            "issue_detected": False,
            "issue_type": "STUCK_NO_PROGRESS",
            "target_file": "dwar_bot/main.py",
            "cursor_prompt": "",
        }
    hard_stuck = any(m in blob for m in stuck_markers)
    soft_only = (not hard_stuck) and any(m in blob for m in soft_stuck)
    no_progress = (
        exp_delta <= 0
        and gold_delta <= 0
        and (
            hard_stuck
            or (soft_only and same_focus >= 5)
            or same_focus >= 5
            or str(tel.get("progress") or "").lower() in ("none", "stuck", "0")
        )
    )
    # World-objective farm window often has 0 exp/gold deltas — don't false-pause
    if "heal_wounded" in blob or "world objective" in blob or "мир-цель" in blob:
        no_progress = hard_stuck and same_focus >= 8
    if no_progress and current_state.upper() not in ("PAUSED", "HEALING"):
        target = "dwar_bot/modules/quest_tracker.py"
        if "hunt" in blob or "combat" in blob or "расселин" in blob:
            target = "dwar_bot/modules/progression_brain.py"
        return {
            "issue_detected": True,
            "issue_type": "STUCK_NO_PROGRESS",
            "target_file": target,
            "cursor_prompt": (
                "Бот без прогресса Exp/Gold за окно аудита (STUCK_NO_PROGRESS). "
                f"state={current_state} telemetry={tel}. "
                f"Устрани зацикливание действий. Лог:\n{clean_slice[-2500:]}"
            ),
        }

    return {
        "issue_detected": False,
        "issue_type": "STUCK_NO_PROGRESS",
        "target_file": "dwar_bot/main.py",
        "cursor_prompt": "",
    }


class GeminiAuditor:
    """Level-1 analyst: Gemini API with heuristic fallback."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "",
        allow_heuristic_fallback: bool = True,
    ) -> None:
        self.api_key = (api_key or _gemini_api_key()).strip()
        self.model = _preferred_model(model)
        self.allow_heuristic_fallback = allow_heuristic_fallback
        self._client = None
        self._auth_fail_until: float = 0.0
        if self.api_key:
            logger.info(
                "GeminiAuditor ready key_kind=%s model=%s",
                _key_kind(self.api_key), self.model,
            )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is empty — add it to .env")
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai is not installed — pip install -U google-genai"
            ) from exc
        # Explicit api_key works for both AQ. (auth) and AIza (standard) keys
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _generate_text(self, client: Any, model_name: str, prompt: str) -> str:
        """Try generate_content, then Interactions API (recommended for new keys)."""
        # 1) Classic generateContent
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            text = getattr(response, "text", None) or ""
            if not text and getattr(response, "candidates", None):
                try:
                    parts = response.candidates[0].content.parts
                    text = "".join(getattr(p, "text", "") or "" for p in parts)
                except Exception:
                    text = ""
            if text:
                return text
        except Exception as exc:
            logger.debug("generate_content(%s) failed: %s", model_name, exc)

        # 2) Interactions API (GA) — preferred path for Auth keys (AQ.)
        try:
            interaction = client.interactions.create(
                model=model_name,
                input=prompt,
            )
            text = getattr(interaction, "output_text", None) or ""
            if text:
                return text
            # Fallback walk steps
            for step in getattr(interaction, "steps", None) or []:
                out = getattr(step, "model_output", None) or getattr(step, "ModelOutput", None)
                if not out:
                    continue
                for content in getattr(out, "content", None) or []:
                    t = getattr(content, "text", None)
                    if t:
                        return str(getattr(t, "text", t) or "")
        except Exception as exc:
            raise RuntimeError(f"interactions+generate_content failed: {exc}") from exc
        return ""

    def audit_bot_health(
        self,
        log_slice: str,
        telemetry_summary: dict,
        current_state: str,
    ) -> Optional[dict]:
        """
        Ask Gemini (or heuristic fallback) for a JSON health verdict.

        Returns normalized dict or ``None`` on total failure.
        """
        # Refresh key each call (AQ. keys may be rotated in .env without restart)
        if not self.api_key:
            self.api_key = _gemini_api_key()
        # Auth dead — don't spam 401 every 120s
        if self.api_key and time.time() < float(self._auth_fail_until or 0):
            if self.allow_heuristic_fallback:
                return _heuristic_audit(log_slice, telemetry_summary, current_state)
            return None
        prompt = _PROMPT_TEMPLATE.format(
            current_state=current_state or "UNKNOWN",
            log_slice=(log_slice or "")[-8000:] or "(empty)",
            telemetry_summary=json.dumps(
                telemetry_summary or {}, ensure_ascii=False, default=str,
            )[:4000],
        )

        if self.api_key:
            try:
                client = self._ensure_client()
                models_try = [self.model] + [
                    m for m in _MODEL_FALLBACKS if m != self.model
                ]
                text = ""
                last_exc: Optional[Exception] = None
                for model_name in models_try:
                    try:
                        text = self._generate_text(client, model_name, prompt)
                        if text:
                            if model_name != self.model:
                                logger.info(
                                    "GeminiAuditor: using fallback model %s",
                                    model_name,
                                )
                            break
                    except Exception as exc:
                        last_exc = exc
                        msg = str(exc)
                        if "401" in msg or "UNAUTHENTICATED" in msg:
                            self._auth_fail_until = time.time() + 3600.0
                            logger.warning(
                                "GeminiAuditor auth dead (401) — backoff 1h, heuristic only"
                            )
                            break
                        logger.warning(
                            "GeminiAuditor model %s failed: %s",
                            model_name, msg[:180],
                        )
                        continue
                else:
                    if last_exc:
                        raise last_exc
                if time.time() < float(self._auth_fail_until or 0):
                    if self.allow_heuristic_fallback:
                        return _heuristic_audit(
                            log_slice, telemetry_summary, current_state
                        )
                    return None
                if not text and last_exc:
                    raise last_exc

                parsed = _extract_json(text)
                if parsed is not None:
                    verdict = _normalize_verdict(parsed)
                    logger.info(
                        "GeminiAuditor: issue_detected=%s type=%s file=%s",
                        verdict.get("issue_detected"),
                        verdict.get("issue_type"),
                        verdict.get("target_file"),
                    )
                    return verdict
                logger.warning(
                    "GeminiAuditor: non-JSON response, falling back. raw=%s",
                    (text or "")[:300],
                )
            except Exception as exc:
                msg = str(exc)
                if "401" in msg or "UNAUTHENTICATED" in msg:
                    self._auth_fail_until = time.time() + 3600.0
                logger.warning("GeminiAuditor API failed: %s", msg[:200])

        if not self.allow_heuristic_fallback:
            return None
        logger.info("GeminiAuditor: using heuristic fallback (no Gemini / bad JSON).")
        return _heuristic_audit(log_slice, telemetry_summary, current_state)