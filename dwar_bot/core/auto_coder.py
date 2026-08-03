"""
Autonomous AutoCoder — Cursor CLI дописывает / чинит код при зависании или краше.

Pipeline
--------
1. Load ``CURSOR_API_KEY`` from ``.env`` (never hardcode secrets).
2. ``check_health()`` every 120–300s: scan ``bot.log`` + telemetry DB.
3. On ``CRASH`` / ``NO_PROGRESS`` → ``fix_and_complete_code()`` via Cursor Agent CLI.
4. Validate with ``ast.parse`` (+ safety scan), run ``pytest tests/test_bot.py``.
5. Success → restart service + Telegram report; failure → ``git checkout --`` rollback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from dwar_bot.core.self_healing.ast_checker import validate_python_code

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
PKG_ROOT: Path = Path(__file__).resolve().parents[1]
DEFAULT_LOG: Path = PKG_ROOT / "logs" / "bot.log"
TEST_TARGET = "tests/test_bot.py"
AGENT_TIMEOUT_SEC = 180
HEALTH_INTERVAL_MIN = 120
HEALTH_INTERVAL_MAX = 300
STUCK_WINDOW_SEC = 300.0  # 5 minutes
COOLDOWN_AFTER_FIX_SEC = 900.0
SERVICE_NAME = os.getenv("DWAR_SERVICE_NAME", "dwar_bot.service")

_TRACE_FILE_RE = re.compile(
    r'File "([^"]+)", line (\d+)',
    re.MULTILINE,
)
_STATUS_LINE_RE = re.compile(
    r"\[\d+\]\s+\S+\s+Lv(\d+)\s*\|\s*HP\s+(\d+)/(\d+).*?area=(\S+)\s*\|\s*"
    r"([\d.]+)\s*зол",
    re.IGNORECASE,
)
_ANALYTICS_RE = re.compile(
    r"Analytics:\s*Lv(\d+)\(([\d.]+)%\).*?Exp/h=([\d./]+).*?Gold/h=([\d.]+).*?battles=(\d+)",
    re.IGNORECASE,
)
_CRASH_MARKERS = (
    "Traceback (most recent call last):",
    "CRITICAL",
    "| CRITICAL |",
    "\x1b[41mCRITICAL",
    "Unhandled exception",
    "Task exception was never retrieved",
)


NotifyFn = Callable[[str], Awaitable[None]]


@dataclass
class HealthIssue:
    issue_type: str  # CRASH | NO_PROGRESS
    reason: str
    target_file: str = "dwar_bot/main.py"
    evidence: str = ""
    detected_at: float = field(default_factory=time.time)


@dataclass
class ProgressSnapshot:
    ts: float
    level: int = 0
    hp: int = 0
    hp_max: int = 0
    gold: float = 0.0
    area_id: str = ""
    exp_proxy: float = 0.0
    battles: int = 0
    status_fingerprint: str = ""


class AutoCoder:
    """
    Stuck & Error detector + Cursor Agent CLI fixer with AST/pytest gates.
    """

    def __init__(
        self,
        *,
        log_path: Optional[Path] = None,
        telemetry_db: Optional[Path] = None,
        repo_root: Optional[Path] = None,
        notify_fn: Optional[NotifyFn] = None,
        interval_min: int = HEALTH_INTERVAL_MIN,
        interval_max: int = HEALTH_INTERVAL_MAX,
        stuck_window_sec: float = STUCK_WINDOW_SEC,
        agent_timeout_sec: int = AGENT_TIMEOUT_SEC,
        dry_run: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root else REPO_ROOT
        self.log_path = Path(log_path) if log_path else self._default_log()
        self.telemetry_db = Path(telemetry_db) if telemetry_db else None
        self.notify_fn = notify_fn
        self.interval_min = max(30, int(interval_min))
        self.interval_max = max(self.interval_min, int(interval_max))
        self.stuck_window_sec = float(stuck_window_sec)
        self.agent_timeout_sec = int(agent_timeout_sec)
        self.dry_run = bool(dry_run) or os.getenv("DWAR_AUTO_CODER_DRY_RUN", "0") == "1"

        self._stop = asyncio.Event()
        self._busy = False
        self._last_fix_at = 0.0
        self._last_crash_hash = ""
        self._progress_history: list[ProgressSnapshot] = []
        self.stats: dict[str, int] = {
            "checks": 0,
            "crashes": 0,
            "no_progress": 0,
            "fixes_ok": 0,
            "fixes_fail": 0,
            "rollbacks": 0,
        }

        try:
            self.load_api_key()
            key_state = "set"
        except Exception as exc:
            key_state = f"MISSING ({exc})"
        logger.info(
            "AutoCoder ready log=%s interval=%d-%ds dry_run=%s key=%s",
            self.log_path,
            self.interval_min,
            self.interval_max,
            self.dry_run,
            key_state if key_state == "set" else "MISSING",
        )

    # ------------------------------------------------------------------
    # .env / key
    # ------------------------------------------------------------------

    def _default_log(self) -> Path:
        try:
            from dwar_bot.config import LOG_FILE
            return Path(LOG_FILE)
        except Exception:
            return DEFAULT_LOG

    def load_api_key(self) -> str:
        """Read ``CURSOR_API_KEY`` from ``.env`` into ``os.environ`` (no hardcode)."""
        try:
            from dwar_bot.core.cursor_self_healer import _load_dotenv, _ensure_cursor_api_key

            _load_dotenv(overwrite=False)
            return _ensure_cursor_api_key()
        except Exception:
            pass
        candidates = [
            self.repo_root / ".env",
            self.repo_root / "dwar_bot" / ".env",
            PKG_ROOT / ".env",
            Path("/root/dwar_bot/.env"),
            Path.cwd() / ".env",
        ]
        for target in candidates:
            if not target.is_file():
                continue
            try:
                for raw in target.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.strip().strip("'").strip('"')
                    if k == "CURSOR_API_KEY" and v:
                        if not (os.environ.get("CURSOR_API_KEY") or "").strip():
                            os.environ["CURSOR_API_KEY"] = v
            except OSError as exc:
                logger.debug("AutoCoder .env read %s: %s", target, exc)
        key = (os.environ.get("CURSOR_API_KEY") or "").strip()
        if not key:
            raise RuntimeError(
                "CURSOR_API_KEY is empty — add it to .env before AutoCoder runs."
            )
        os.environ["CURSOR_API_KEY"] = key
        return key

    # ------------------------------------------------------------------
    # Health detector
    # ------------------------------------------------------------------

    def check_health(self) -> Optional[HealthIssue]:
        """
        Scan bot.log + telemetry. Return a HealthIssue or None if healthy.
        Priority: CRASH > NO_PROGRESS.
        """
        self.stats["checks"] += 1
        # Known Flash side-quest noise is NOT a crash / stuck code bug
        if self._suppress_known_flash_noise():
            return None
        crash = self._detect_crash()
        if crash:
            self.stats["crashes"] += 1
            return crash
        stuck = self._detect_no_progress()
        if stuck:
            self.stats["no_progress"] += 1
            return stuck
        return None

    def _suppress_known_flash_noise(self) -> bool:
        """
        Self-heal path for heal_wounded / «Не задано действие!».

        Locks Flash WO in state.json and skips Cursor escalate — HTTP USE
        cannot target canvas wounded soldiers.
        """
        text = self._read_log_tail(max_bytes=60_000)
        if not text:
            return False
        low = text.lower()
        markers = (
            "не задано действие",
            "heal_wounded: http use rejected",
            "heal_wounded: flash-only",
            "world objective set kind=heal_wounded",
        )
        hits = sum(1 for m in markers if m in low)
        if hits < 1 and "heal_wounded" not in low:
            return False
        # Recent open-farm activity ⇒ healthy despite Flash WO
        farming = any(
            m in low
            for m in (
                "post-hunt open-farm",
                "telemetry battle win",
                "hunt_farm lv",
                "flash-farm-lv3",
            )
        )
        locked = self._lock_flash_objective_state()
        if locked or farming or hits >= 1:
            if locked:
                logger.info(
                    "AutoCoder: locked heal_wounded flash/http_impossible in state "
                    "(no Cursor — protocol is client-only)."
                )
                self.stats["fixes_ok"] = self.stats.get("fixes_ok", 0) + (1 if locked else 0)
            # Only fully suppress escalate when farming or we locked state
            if farming or locked:
                return True
        return False

    def _lock_flash_objective_state(self) -> bool:
        """Patch persisted world_objective so the running bot stops USE probes."""
        candidates = [
            self.repo_root / "dwar_bot" / "state.json",
            self.repo_root / "dwar_bot" / "data" / "state.json",
            PKG_ROOT / "state.json",
            PKG_ROOT / "data" / "state.json",
            Path("/root/dwar_bot/state.json"),
            Path("/root/dwar_bot/data/state.json"),
        ]
        try:
            from dwar_bot.config import STATE_FILE
            candidates.insert(0, Path(STATE_FILE))
        except Exception:
            pass
        changed = False
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                wo = data.get("world_objective")
                if not isinstance(wo, dict) or wo.get("kind") != "heal_wounded":
                    continue
                before = json.dumps(wo, sort_keys=True)
                wo["flash_only"] = True
                wo["http_impossible"] = True
                wo["flash_notified"] = True
                wo["protocol_fail_until"] = time.time() + 86400.0
                data["world_objective"] = wo
                after = json.dumps(wo, sort_keys=True)
                if after != before:
                    path.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    changed = True
                    logger.info("AutoCoder: patched %s heal_wounded lock", path)
            except Exception as exc:
                logger.debug("AutoCoder state lock %s: %s", path, exc)
        return changed

    def _read_log_tail(self, max_bytes: int = 120_000) -> str:
        path = self.log_path
        if not path.is_file():
            return ""
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > max_bytes:
                    fh.seek(-max_bytes, os.SEEK_END)
                data = fh.read()
            return data.decode("utf-8", errors="replace")
        except OSError as exc:
            logger.debug("AutoCoder log read: %s", exc)
            return ""

    def _detect_crash(self) -> Optional[HealthIssue]:
        text = self._read_log_tail()
        if not text:
            return None
        # Prefer the last Traceback block
        idx = text.rfind("Traceback (most recent call last):")
        block = ""
        if idx >= 0:
            block = text[idx: idx + 3500]
        else:
            # CRITICAL / ERROR with exception class
            for marker in _CRASH_MARKERS:
                pos = text.rfind(marker)
                if pos < 0:
                    continue
                # Ignore AutoCoder / healer self-noise
                window = text[max(0, pos - 200): pos + 800]
                if any(
                    n in window
                    for n in (
                        "AutoCoder",
                        "cursor_self_healer",
                        "HealingOrchestrator",
                        "GeminiAuditor",
                        "AutoHealer",
                    )
                ):
                    continue
                if "ERROR" in marker or "CRITICAL" in marker or "Traceback" in marker:
                    # Require a real exception-ish line nearby
                    snippet = text[pos: pos + 1200]
                    if re.search(
                        r"(Error|Exception|Traceback|CRITICAL)",
                        snippet,
                    ):
                        block = snippet
                        break
        if not block:
            return None

        # AUTH / cookie waits are operational, not code crashes
        low = block.lower()
        if any(
            k in low
            for k in (
                "tokenexpired",
                "oauth access_token expired",
                "paste fresh cookies",
                "waiting for fresh cookies",
                "no cookies yet",
            )
        ):
            return None

        digest = hashlib.sha256(block.encode("utf-8", errors="replace")).hexdigest()[:16]
        if digest == self._last_crash_hash and (time.time() - self._last_fix_at) < COOLDOWN_AFTER_FIX_SEC:
            return None

        target = self._infer_target_from_traceback(block) or "dwar_bot/main.py"
        return HealthIssue(
            issue_type="CRASH",
            reason="Traceback / CRITICAL в bot.log",
            target_file=target,
            evidence=block[-2000:],
        )

    def _infer_target_from_traceback(self, block: str) -> str:
        matches = list(_TRACE_FILE_RE.finditer(block))
        # Prefer last frame inside dwar_bot/
        for m in reversed(matches):
            raw = m.group(1).replace("\\", "/")
            if "dwar_bot/" in raw:
                idx = raw.find("dwar_bot/")
                return raw[idx:]
            if raw.endswith(".py") and "/dwar_bot/" not in raw:
                name = Path(raw).name
                for cand in (
                    self.repo_root / "dwar_bot" / name,
                    self.repo_root / "dwar_bot" / "modules" / name,
                    self.repo_root / "dwar_bot" / "core" / name,
                ):
                    if cand.is_file():
                        try:
                            return str(cand.relative_to(self.repo_root))
                        except ValueError:
                            return str(cand)
        if matches:
            raw = matches[-1].group(1).replace("\\", "/")
            if raw.endswith(".py"):
                return raw if raw.startswith("dwar_bot/") else f"dwar_bot/{Path(raw).name}"
        return "dwar_bot/main.py"

    def _detect_no_progress(self) -> Optional[HealthIssue]:
        snap = self._collect_progress_snapshot()
        if snap is None:
            return None
        self._progress_history.append(snap)
        # Keep ~20 minutes of history
        cutoff = time.time() - max(self.stuck_window_sec * 4, 1200)
        self._progress_history = [s for s in self._progress_history if s.ts >= cutoff]
        if len(self._progress_history) < 2:
            return None

        oldest = None
        for s in self._progress_history:
            if snap.ts - s.ts >= self.stuck_window_sec * 0.9:
                oldest = s
                break
        if oldest is None:
            return None

        changed = (
            snap.gold != oldest.gold
            or snap.hp != oldest.hp
            or snap.level != oldest.level
            or snap.area_id != oldest.area_id
            or snap.exp_proxy != oldest.exp_proxy
            or snap.battles != oldest.battles
            or snap.status_fingerprint != oldest.status_fingerprint
        )
        if changed:
            return None

        # Intentional idle states are not "stuck code"
        try:
            from dwar_bot.core.bot_state import BotState, get_bot_state
            st = get_bot_state()
            if st in (BotState.PAUSED, BotState.HEALING):
                return None
        except Exception:
            pass

        reason = (
            f"За {int(self.stuck_window_sec)}с нет изменений Exp/Gold/HP/area/battles "
            f"(gold={snap.gold}, hp={snap.hp}/{snap.hp_max}, area={snap.area_id}, "
            f"battles={snap.battles}, fp={snap.status_fingerprint[:40]})"
        )
        target = self._guess_stuck_target()
        return HealthIssue(
            issue_type="NO_PROGRESS",
            reason=reason,
            target_file=target,
            evidence=reason,
        )

    def _guess_stuck_target(self) -> str:
        text = self._read_log_tail(60_000).lower()
        mapping = (
            ("hunt_farm", "dwar_bot/modules/combat_engine.py"),
            ("suis hunt", "dwar_bot/modules/combat_engine.py"),
            ("flash-farm", "dwar_bot/modules/leveling_engine.py"),
            ("world_obj", "dwar_bot/modules/leveling_engine.py"),
            ("quest", "dwar_bot/modules/quest_tracker.py"),
            ("переход", "dwar_bot/main.py"),
            ("travel", "dwar_bot/main.py"),
            ("fight", "dwar_bot/modules/fight_client.py"),
        )
        for needle, path in mapping:
            if needle in text:
                return path
        return "dwar_bot/main.py"

    def _collect_progress_snapshot(self) -> Optional[ProgressSnapshot]:
        now = time.time()
        # 1) Parse latest status line from bot.log
        text = self._read_log_tail(80_000)
        level = hp = hp_max = 0
        gold = 0.0
        area = ""
        fp = ""
        for m in _STATUS_LINE_RE.finditer(text):
            level = int(m.group(1))
            hp = int(m.group(2))
            hp_max = int(m.group(3))
            area = m.group(4).rstrip("…")
            gold = float(m.group(5))
            fp = m.group(0)[:120]
        exp_proxy = 0.0
        battles = 0
        for m in _ANALYTICS_RE.finditer(text):
            try:
                exp_proxy = float(str(m.group(2)))
            except ValueError:
                pass
            try:
                battles = int(m.group(5))
            except ValueError:
                pass

        # 2) Telemetry DB (gold / battles / exp_proxy)
        db = self._resolve_telemetry_db()
        if db and db.is_file():
            try:
                with sqlite3.connect(str(db), timeout=5) as con:
                    con.row_factory = sqlite3.Row
                    row = con.execute(
                        "SELECT * FROM economy_snapshots ORDER BY ts DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        gold = float(row["gold"] or gold)
                        exp_proxy = float(row["exp_proxy"] or exp_proxy)
                        battles = int(row["battles"] or battles)
                    bcount = con.execute(
                        "SELECT COUNT(*) AS c FROM battle_events "
                        "WHERE finished_at > ?",
                        (now - self.stuck_window_sec,),
                    ).fetchone()
                    if bcount and int(bcount["c"] or 0) > 0:
                        # Recent battles ⇒ progress exists even if gold flat
                        battles = max(battles, int(bcount["c"]))
            except Exception as exc:
                logger.debug("AutoCoder telemetry: %s", exc)

        if not fp and gold == 0 and hp == 0 and battles == 0:
            return None
        return ProgressSnapshot(
            ts=now,
            level=level,
            hp=hp,
            hp_max=hp_max,
            gold=gold,
            area_id=area,
            exp_proxy=exp_proxy,
            battles=battles,
            status_fingerprint=fp or f"g={gold}|b={battles}|a={area}",
        )

    def _resolve_telemetry_db(self) -> Optional[Path]:
        if self.telemetry_db:
            return self.telemetry_db
        roots = [
            self.repo_root / "data",
            Path("/root/data"),
            PKG_ROOT / "data",
        ]
        newest: Optional[Path] = None
        newest_mtime = 0.0
        for root in roots:
            if not root.is_dir():
                continue
            for p in root.glob("telemetry*.db"):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if m > newest_mtime:
                    newest_mtime = m
                    newest = p
        return newest

    # ------------------------------------------------------------------
    # Prompt + Cursor CLI
    # ------------------------------------------------------------------

    def build_prompt(self, issue: HealthIssue) -> str:
        return (
            "Проект бота для 'Легенды: Наследие Драконов' требует исправления или доработки.\n"
            f"Причина: {issue.issue_type}\n"
            f"Целевой файл: {issue.target_file}\n"
            f"Лог/Симптомы: {issue.reason}\n\n"
            f"Детали:\n{issue.evidence[:2500]}\n\n"
            "Задание:\n"
            "1. Если это ошибка — найди некорректный CSS-селектор, таймаут или логический баг "
            f"и исправь его (смотри также config/selectors.py при необходимости).\n"
            "2. Если это отсутствие прогресса — допиши недостающую логику, обработку зависания "
            f"или смену локации прямо в {issue.target_file}.\n"
            "3. Не ломай существующую структуру методов!\n"
            "4. Минимальный дифф. Не трогай .env и секреты. Сохрани файлы на диск и закончи.\n"
            "Не спрашивай подтверждений — сразу правь код."
        )

    def _augment_env(self) -> dict[str, str]:
        env = os.environ.copy()
        try:
            from dwar_bot.core.cursor_self_healer import _augment_path
            env = _augment_path(env)
        except Exception:
            home = Path.home()
            extras = [
                str(home / ".local" / "bin"),
                str(home / ".cursor" / "bin"),
                "/usr/local/bin",
            ]
            parts = env.get("PATH", os.defpath or "").split(os.pathsep)
            for e in reversed(extras):
                if e and e not in parts:
                    parts.insert(0, e)
            env["PATH"] = os.pathsep.join(parts)
        env["CURSOR_API_KEY"] = (os.environ.get("CURSOR_API_KEY") or "").strip()
        env["CI"] = "1"
        return env

    def _agent_commands(self, prompt: str, env: dict[str, str]) -> list[list[str]]:
        workspace = str(self.repo_root)
        # Prefer the invocation from the task spec; then headless VPS agent.
        cmds = [
            ["cursor", "agent", "--message", prompt],
            ["agent", "-p", "--force", "--trust", "--workspace", workspace, prompt],
            ["cursor-agent", "-p", "--force", "--trust", "--workspace", workspace, prompt],
        ]
        usable: list[list[str]] = []
        for cmd in cmds:
            if shutil.which(cmd[0], path=env.get("PATH")):
                usable.append(cmd)
        return usable

    def invoke_cursor_agent(self, prompt: str) -> subprocess.CompletedProcess:
        """Call Cursor Agent CLI (subprocess, timeout=180s by default)."""
        try:
            from dwar_bot.core.cursor_self_healer import ensure_cursor_cli
            ensure_cursor_cli()
        except Exception as exc:
            logger.warning("AutoCoder ensure_cursor_cli: %s", exc)

        env = self._augment_env()
        if not env.get("CURSOR_API_KEY"):
            self.load_api_key()
            env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]

        cmds = self._agent_commands(prompt, env)
        if not cmds:
            raise FileNotFoundError(
                "Cursor Agent CLI not found (tried cursor/agent/cursor-agent)"
            )

        last: Optional[subprocess.CompletedProcess] = None
        for cmd in cmds:
            logger.info("AutoCoder invoking: %s …", " ".join(cmd[:4]))
            try:
                last = subprocess.run(
                    cmd,
                    env=env,
                    timeout=self.agent_timeout_sec,
                    capture_output=True,
                    text=True,
                    cwd=str(self.repo_root),
                    start_new_session=True,
                )
            except FileNotFoundError:
                continue
            if last.returncode == 0:
                return last
            logger.warning(
                "AutoCoder agent exit=%s stderr=%s",
                last.returncode,
                (last.stderr or "")[:400],
            )
        assert last is not None
        return last

    # ------------------------------------------------------------------
    # Validate / rollback / restart
    # ------------------------------------------------------------------

    def _resolve_target(self, target_file: str) -> Path:
        p = Path(target_file)
        if p.is_absolute():
            return p
        for cand in (
            self.repo_root / p,
            self.repo_root / "dwar_bot" / p,
            PKG_ROOT / p.name,
            PKG_ROOT / "modules" / p.name,
            PKG_ROOT / "core" / p.name,
        ):
            if cand.is_file():
                return cand
        return self.repo_root / p

    def _rel_to_repo(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root.resolve()))
        except ValueError:
            return str(path)

    def validate_syntax(self, target: Path) -> tuple[bool, str]:
        try:
            src = target.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"read failed: {exc}"
        ok, err = validate_python_code(src)
        # Also satisfy the explicit ast.parse requirement
        if ok:
            try:
                import ast
                ast.parse(src)
            except SyntaxError as exc:
                return False, f"ast.parse: {exc}"
        return ok, err or ""

    def run_pytest(self) -> bool:
        env = self._augment_env()
        cmd = [
            os.environ.get("PYTHON", "python3"),
            "-m",
            "pytest",
            TEST_TARGET,
            "-q",
            "--tb=line",
        ]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                env=env,
                timeout=120,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            logger.error("AutoCoder pytest failed to start: %s", exc)
            return False
        logger.info(
            "AutoCoder pytest code=%s\n%s",
            result.returncode,
            ((result.stdout or "") + (result.stderr or ""))[-1500:],
        )
        return result.returncode == 0

    def rollback(self, target: Path) -> None:
        rel = self._rel_to_repo(target)
        git = self.repo_root / ".git"
        if git.exists() or (self.repo_root / ".git").is_file():
            subprocess.run(
                ["git", "checkout", "--", rel],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            logger.warning("AutoCoder rollback: git checkout -- %s", rel)
            self.stats["rollbacks"] += 1
            return
        # Fallback: restore .last.bak from heal backups if present
        bak = PKG_ROOT / ".heal_backups" / f"{target.name}.last.bak"
        if bak.is_file():
            shutil.copy2(bak, target)
            logger.warning("AutoCoder rollback from backup %s", bak)
            self.stats["rollbacks"] += 1

    def restart_bot(self) -> bool:
        if os.getenv("DWAR_RESTART_AFTER_HEAL", "1") == "0":
            logger.info("AutoCoder: restart disabled by DWAR_RESTART_AFTER_HEAL=0")
            return False
        try:
            from dwar_bot.core.cursor_self_healer import mark_skip_boot_scan, _restart_service
            mark_skip_boot_scan()
            return bool(_restart_service())
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["systemctl", "restart", SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=60,
            )
            ok = result.returncode == 0
            logger.info("AutoCoder systemctl restart → %s", ok)
            return ok
        except Exception as exc:
            logger.error("AutoCoder restart failed: %s", exc)
            return False

    async def _notify(self, text: str) -> None:
        if not self.notify_fn:
            logger.warning("AutoCoder notify (no TG): %s", text[:240])
            return
        try:
            await self.notify_fn(text)
        except Exception as exc:
            logger.warning("AutoCoder notify failed: %s", exc)

    # ------------------------------------------------------------------
    # Main fix pipeline
    # ------------------------------------------------------------------

    def fix_and_complete_code(self, issue: HealthIssue) -> bool:
        """
        Snapshot → Cursor CLI → ast.parse → pytest → restart / rollback.
        Synchronous (run via ``asyncio.to_thread`` from the loop).
        """
        target = self._resolve_target(issue.target_file)
        if not target.is_file():
            logger.error("AutoCoder target missing: %s", target)
            return False

        try:
            self.load_api_key()
        except Exception as exc:
            logger.error("AutoCoder: %s", exc)
            return False

        before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        # Local snapshot for non-git environments
        backup_dir = PKG_ROOT / ".heal_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{target.name}.{int(time.time())}.bak"
        shutil.copy2(target, backup)
        last = backup_dir / f"{target.name}.last.bak"
        shutil.copy2(target, last)

        prompt = self.build_prompt(issue)
        if self.dry_run:
            logger.info("AutoCoder DRY-RUN — would fix %s (%s)", target, issue.issue_type)
            return False

        try:
            result = self.invoke_cursor_agent(prompt)
        except subprocess.TimeoutExpired:
            logger.error("AutoCoder: Cursor CLI timed out — rollback")
            self.rollback(target)
            return False
        except Exception as exc:
            logger.exception("AutoCoder: Cursor CLI failed: %s", exc)
            self.rollback(target)
            return False

        logger.info(
            "AutoCoder agent finished code=%s\n%s",
            result.returncode,
            ((result.stdout or "") + "\n" + (result.stderr or ""))[-2000:],
        )
        if result.returncode != 0:
            self.rollback(target)
            return False

        after_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if after_hash == before_hash:
            logger.error("AutoCoder NO-OP: file unchanged — not SUCCESS")
            self.rollback(target)
            return False

        ok, err = self.validate_syntax(target)
        if not ok:
            logger.error("AutoCoder AST/safety failed: %s", err)
            self.rollback(target)
            return False

        if not self.run_pytest():
            logger.error("AutoCoder pytest failed — rollback")
            self.rollback(target)
            return False

        logger.info("AutoCoder SUCCESS for %s (%s)", target, issue.issue_type)
        self._last_fix_at = time.time()
        if issue.issue_type == "CRASH" and issue.evidence:
            self._last_crash_hash = hashlib.sha256(
                issue.evidence.encode("utf-8", errors="replace")
            ).hexdigest()[:16]
        self.restart_bot()
        return True

    async def handle_issue(self, issue: HealthIssue) -> bool:
        if self._busy:
            return False
        if time.time() - self._last_fix_at < COOLDOWN_AFTER_FIX_SEC:
            logger.info("AutoCoder cooldown active — skip fix")
            return False
        self._busy = True
        try:
            await self._notify(
                f"🛠 <b>AutoCoder START</b>\n"
                f"type=<code>{issue.issue_type}</code>\n"
                f"file=<code>{issue.target_file}</code>\n"
                f"{issue.reason[:300]}"
            )
            ok = await asyncio.to_thread(self.fix_and_complete_code, issue)
            if ok:
                self.stats["fixes_ok"] += 1
                await self._notify(
                    f"✅ <b>AutoCoder SUCCESS</b>\n"
                    f"type=<code>{issue.issue_type}</code>\n"
                    f"file=<code>{issue.target_file}</code>\n"
                    f"Бот перезапущен после pytest."
                )
            else:
                self.stats["fixes_fail"] += 1
                await self._notify(
                    f"🔄 <b>AutoCoder FAIL / rollback</b>\n"
                    f"type=<code>{issue.issue_type}</code>\n"
                    f"file=<code>{issue.target_file}</code>"
                )
            return ok
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        """Background health loop — interval randomly in [120, 300] seconds."""
        logger.info(
            "AutoCoder loop started — interval %d–%ds",
            self.interval_min,
            self.interval_max,
        )
        try:
            while not self._stop.is_set():
                delay = random.uniform(self.interval_min, self.interval_max)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    from dwar_bot.core.bot_state import BotState, get_bot_state
                    if get_bot_state() in (BotState.PAUSED, BotState.HEALING):
                        continue
                except Exception:
                    pass
                try:
                    issue = await asyncio.to_thread(self.check_health)
                    if issue:
                        logger.warning(
                            "AutoCoder detected %s → %s",
                            issue.issue_type,
                            issue.target_file,
                        )
                        await self.handle_issue(issue)
                except Exception as exc:
                    logger.exception("AutoCoder tick error: %s", exc)
        except asyncio.CancelledError:
            logger.info("AutoCoder cancelled.")
            raise
        finally:
            logger.info("AutoCoder stopped. stats=%s", self.stats)


# ---------------------------------------------------------------------------
# Convenience helpers for main.py
# ---------------------------------------------------------------------------

_AUTO_CODER: Optional[AutoCoder] = None


def get_auto_coder() -> Optional[AutoCoder]:
    return _AUTO_CODER


def bind_auto_coder(**kwargs: Any) -> AutoCoder:
    global _AUTO_CODER
    _AUTO_CODER = AutoCoder(**kwargs)
    return _AUTO_CODER


async def start_auto_coder(
    *,
    notify_fn: Optional[NotifyFn] = None,
    log_path: Optional[Path] = None,
    telemetry_db: Optional[Path] = None,
    interval_min: int = HEALTH_INTERVAL_MIN,
    interval_max: int = HEALTH_INTERVAL_MAX,
) -> AutoCoder:
    """Create AutoCoder and return it (caller should ``create_task(run_forever)``)."""
    coder = bind_auto_coder(
        notify_fn=notify_fn,
        log_path=log_path,
        telemetry_db=telemetry_db,
        interval_min=interval_min,
        interval_max=interval_max,
    )
    return coder
