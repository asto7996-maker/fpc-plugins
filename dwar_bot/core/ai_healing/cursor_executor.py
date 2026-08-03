"""
CursorExecutor — Level-2 patch applier (Cursor CLI + AST + pytest + git rollback).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
BACKUP_ROOT: Path = REPO_ROOT / "data" / "backups"
TEST_TARGET = "tests/test_bot.py"
AGENT_TIMEOUT_SEC = 180


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


def _ensure_cursor_api_key() -> str:
    _load_dotenv()
    key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("CURSOR_API_KEY is empty — add it to .env before healing.")
    os.environ["CURSOR_API_KEY"] = key
    return key


def _augment_path(env: dict) -> dict:
    try:
        from dwar_bot.core.cursor_self_healer import _augment_path as _shared

        return _shared(env)
    except Exception:
        home = Path.home()
        extras = [
            str(home / ".local" / "bin"),
            str(home / ".cursor" / "bin"),
            "/usr/local/bin",
        ]
        current = env.get("PATH", os.defpath or "")
        parts = current.split(os.pathsep)
        for e in reversed(extras):
            if e and e not in parts:
                parts.insert(0, e)
        env["PATH"] = os.pathsep.join(parts)
        return env


def _resolve_target(target_file: str) -> Path:
    p = Path(target_file)
    if p.is_absolute():
        try:
            return p.relative_to(REPO_ROOT)
        except ValueError:
            return p
    candidates = [
        REPO_ROOT / p,
        REPO_ROOT / "dwar_bot" / p,
        REPO_ROOT / "dwar_bot" / "modules" / p.name,
        REPO_ROOT / "dwar_bot" / "core" / p.name,
        REPO_ROOT / "config" / p.name,
    ]
    for c in candidates:
        if c.exists():
            try:
                return c.relative_to(REPO_ROOT)
            except ValueError:
                return c
    return p


def _abs(rel: Path) -> Path:
    return rel if rel.is_absolute() else (REPO_ROOT / rel)


class CursorExecutor:
    """Apply Cursor CLI patches with AST/pytest gates and git rollback."""

    def __init__(self, *, timeout_sec: int = AGENT_TIMEOUT_SEC) -> None:
        self.timeout_sec = int(timeout_sec or AGENT_TIMEOUT_SEC)
        self.last_error: str = ""

    def _create_backup(self, rel: Path) -> Path:
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        src = _abs(rel)
        stamp = int(time.time())
        slug = str(rel).replace("\\", "/").replace("/", "__")
        dst = BACKUP_ROOT / f"{slug}.bak_{stamp}"
        if src.exists():
            shutil.copy2(src, dst)
            logger.info("CursorExecutor backup: %s → %s", rel, dst)
        return dst

    def _git_checkout(self, rel: Path) -> None:
        if not (REPO_ROOT / ".git").exists():
            logger.warning("CursorExecutor: no .git — cannot git checkout %s", rel)
            return
        subprocess.run(
            ["git", "checkout", "--", str(rel)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        logger.warning("CursorExecutor: git checkout -- %s", rel)

    def _restore(self, rel: Path, backup: Path) -> None:
        src = _abs(rel)
        if backup.exists():
            shutil.copy2(backup, src)
            logger.warning("CursorExecutor: restored %s from backup", rel)
        self._git_checkout(rel)

    def _run_cursor_cli(self, message: str, env: dict) -> subprocess.CompletedProcess:
        workspace = str(REPO_ROOT)
        attempts = [
            ["cursor", "agent", "--message", message],
            ["agent", "-p", "--force", "--trust", "--workspace", workspace, message],
            ["cursor-agent", "-p", "--force", "--trust", "--workspace", workspace, message],
        ]
        last: Optional[subprocess.CompletedProcess] = None
        for cmd in attempts:
            if shutil.which(cmd[0], path=env.get("PATH")) is None:
                continue
            logger.info("CursorExecutor: invoking %s …", cmd[0])
            try:
                last = subprocess.run(
                    cmd,
                    env=env,
                    timeout=self.timeout_sec,
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                )
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                self.last_error = f"Cursor CLI timed out ({self.timeout_sec}s)"
                raise
            if last.returncode == 0:
                return last
            logger.warning(
                "CursorExecutor: %s exit=%s stderr=%s",
                cmd[0],
                last.returncode,
                (last.stderr or "")[:400],
            )
        if last is None:
            raise FileNotFoundError(
                "Cursor CLI not found (tried: cursor, agent, cursor-agent)"
            )
        return last

    def _ast_ok(self, path: Path) -> bool:
        try:
            from dwar_bot.core.self_healing.ast_checker import validate_python_code

            code = path.read_text(encoding="utf-8")
            ok, err = validate_python_code(code)
            if not ok:
                self.last_error = err or "AST validation failed"
                return False
            return True
        except Exception:
            import ast

            try:
                ast.parse(path.read_text(encoding="utf-8"))
                return True
            except SyntaxError as exc:
                self.last_error = f"SyntaxError: {exc}"
                return False

    def _pytest_ok(self, env: dict) -> bool:
        env = dict(env)
        pp = env.get("PYTHONPATH", "")
        root = str(REPO_ROOT)
        if root not in pp.split(os.pathsep):
            env["PYTHONPATH"] = root + (os.pathsep + pp if pp else "")

        for venv_py in (
            Path("/opt/dwar_venv/bin/python"),
            REPO_ROOT / ".venv" / "bin" / "python",
        ):
            if venv_py.exists():
                cmd = [str(venv_py), "-m", "pytest", TEST_TARGET, "-q", "--tb=short"]
                break
        else:
            pytest_bin = shutil.which("pytest", path=env.get("PATH"))
            if pytest_bin:
                cmd = [pytest_bin, TEST_TARGET, "-q", "--tb=short"]
            else:
                cmd = ["python3", "-m", "pytest", TEST_TARGET, "-q", "--tb=short"]

        logger.info("CursorExecutor: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.last_error = "pytest timed out"
            return False
        if proc.returncode != 0:
            self.last_error = (proc.stdout or "")[-500:] + (proc.stderr or "")[-500:]
            return False
        return True

    def execute_patch(
        self,
        target_file: str,
        cursor_prompt: str,
        raw_error: str,
    ) -> bool:
        """
        Backup → Cursor CLI → ast.parse → pytest → keep or git rollback.

        Returns True on successful tested patch.
        """
        self.last_error = ""
        try:
            _ensure_cursor_api_key()
        except RuntimeError as exc:
            self.last_error = str(exc)
            logger.error("CursorExecutor: %s", exc)
            return False

        rel = _resolve_target(target_file)
        abs_path = _abs(rel)
        if not abs_path.exists():
            self.last_error = f"target file missing: {rel}"
            logger.error("CursorExecutor: %s", self.last_error)
            return False

        backup = self._create_backup(rel)
        message = (
            f"{cursor_prompt}\n"
            f" Файл сбоя: {rel}\n"
            f" Лог: {raw_error[-4000:]}"
        )
        env = _augment_path(os.environ.copy())
        env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]

        try:
            result = self._run_cursor_cli(message, env)
            if result.returncode != 0:
                self.last_error = (
                    f"CLI exit={result.returncode} "
                    f"stderr={(result.stderr or '')[:500]}"
                )
                self._restore(rel, backup)
                return False
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("CursorExecutor CLI failed")
            self._restore(rel, backup)
            return False

        if not self._ast_ok(abs_path):
            logger.error("CursorExecutor: AST failed — rollback (%s)", self.last_error)
            self._restore(rel, backup)
            return False

        if not self._pytest_ok(env):
            logger.error("CursorExecutor: pytest failed — rollback")
            self._restore(rel, backup)
            # Explicit git checkout as required by the contract
            self._git_checkout(rel)
            return False

        try:
            if backup.exists():
                backup.unlink()
        except OSError:
            pass
        logger.info("CursorExecutor: patch OK for %s", rel)
        return True
