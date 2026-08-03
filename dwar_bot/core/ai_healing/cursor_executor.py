"""
CursorExecutor — Level-2 patch applier (Cursor CLI + AST + pytest + rollback).

Supports both monorepo and VPS ``/root/dwar_bot`` layouts.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from dwar_bot.core.ai_healing.paths import resolve_repo_root, resolve_test_target

logger = logging.getLogger(__name__)

REPO_ROOT: Path = resolve_repo_root()
BACKUP_ROOT: Path = REPO_ROOT / "data" / "backups"
AGENT_TIMEOUT_SEC = 150


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


def _resolve_target(target_file: str, repo_root: Path) -> Path:
    p = Path(target_file)
    if p.is_absolute():
        try:
            return p.relative_to(repo_root)
        except ValueError:
            # Absolute under dwar_bot package
            try:
                return p.relative_to(repo_root.parent)
            except ValueError:
                return p
    # Strip leading dwar_bot/ when workspace IS dwar_bot
    parts = p.parts
    if repo_root.name == "dwar_bot" and parts and parts[0] == "dwar_bot":
        p = Path(*parts[1:]) if len(parts) > 1 else Path(".")
    candidates = [
        repo_root / p,
        repo_root / "dwar_bot" / p,
        repo_root / "modules" / Path(p).name,
        repo_root / "core" / Path(p).name,
        repo_root / "dwar_bot" / "modules" / Path(p).name,
        repo_root / "dwar_bot" / "core" / Path(p).name,
        repo_root / "config" / Path(p).name,
    ]
    for c in candidates:
        if c.exists():
            try:
                return c.relative_to(repo_root)
            except ValueError:
                return c
    return Path(target_file)


def _abs(repo_root: Path, rel: Path) -> Path:
    return rel if rel.is_absolute() else (repo_root / rel)


class CursorExecutor:
    """Apply Cursor CLI patches with AST/pytest gates and backup rollback."""

    def __init__(self, *, timeout_sec: int = AGENT_TIMEOUT_SEC) -> None:
        self.timeout_sec = int(timeout_sec or AGENT_TIMEOUT_SEC)
        self.last_error: str = ""
        self.repo_root = resolve_repo_root()

    def _create_backup(self, rel: Path) -> Path:
        backup_root = self.repo_root / "data" / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        src = _abs(self.repo_root, rel)
        stamp = int(time.time())
        slug = str(rel).replace("\\", "/").replace("/", "__")
        dst = backup_root / f"{slug}.bak_{stamp}"
        if src.exists():
            shutil.copy2(src, dst)
            logger.info("CursorExecutor backup: %s → %s", rel, dst)
        return dst

    def _git_checkout(self, rel: Path) -> None:
        if not (self.repo_root / ".git").exists():
            return
        subprocess.run(
            ["git", "checkout", "--", str(rel)],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        logger.warning("CursorExecutor: git checkout -- %s", rel)

    def _restore(self, rel: Path, backup: Path) -> None:
        src = _abs(self.repo_root, rel)
        if backup.exists():
            shutil.copy2(backup, src)
            logger.warning("CursorExecutor: restored %s from backup", rel)
        self._git_checkout(rel)

    def _run_cursor_cli(self, message: str, env: dict) -> subprocess.CompletedProcess:
        workspace = str(self.repo_root)
        # Prefer headless agent (VPS); desktop ``cursor agent`` is optional
        attempts = [
            ["agent", "-p", "--force", "--trust", "--workspace", workspace, message],
            ["cursor-agent", "-p", "--force", "--trust", "--workspace", workspace, message],
            ["cursor", "agent", "--message", message],
        ]
        last: Optional[subprocess.CompletedProcess] = None
        for cmd in attempts:
            if shutil.which(cmd[0], path=env.get("PATH")) is None:
                continue
            logger.info(
                "CursorExecutor: invoking %s (workspace=%s) …",
                cmd[0], workspace,
            )
            try:
                # New session so timeout can kill the whole process group
                last = subprocess.run(
                    cmd,
                    env=env,
                    timeout=self.timeout_sec,
                    capture_output=True,
                    text=True,
                    cwd=workspace,
                    start_new_session=True,
                )
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired as exc:
                self.last_error = f"Cursor CLI timed out ({self.timeout_sec}s)"
                # Best-effort kill process group
                try:
                    if exc.process is not None and exc.process.pid:
                        os.killpg(exc.process.pid, signal.SIGKILL)
                except Exception:
                    pass
                # Also pkill stray agents
                subprocess.run(
                    ["pkill", "-f", "cursor-agent/versions"],
                    capture_output=True, check=False,
                )
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
                "Cursor CLI not found (tried: agent, cursor-agent, cursor)"
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
        # Ensure both repo root and dwar_bot parent are importable
        roots = [str(self.repo_root)]
        if self.repo_root.name == "dwar_bot":
            roots.append(str(self.repo_root.parent))
        for r in roots:
            if r not in pp.split(os.pathsep):
                pp = r + (os.pathsep + pp if pp else "")
        env["PYTHONPATH"] = pp

        test_target = resolve_test_target(self.repo_root)
        for venv_py in (
            Path("/opt/dwar_venv/bin/python"),
            self.repo_root / ".venv" / "bin" / "python",
        ):
            if venv_py.exists():
                cmd = [str(venv_py), "-m", "pytest", test_target, "-q", "--tb=short"]
                break
        else:
            pytest_bin = shutil.which("pytest", path=env.get("PATH"))
            if pytest_bin:
                cmd = [pytest_bin, test_target, "-q", "--tb=short"]
            else:
                cmd = ["python3", "-m", "pytest", test_target, "-q", "--tb=short"]

        logger.info("CursorExecutor: %s (cwd=%s)", " ".join(cmd), self.repo_root)
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.last_error = "pytest timed out"
            return False
        if proc.returncode != 0:
            self.last_error = (proc.stdout or "")[-500:] + (proc.stderr or "")[-500:]
            logger.error("CursorExecutor pytest FAILED: %s", self.last_error[:400])
            return False
        logger.info("CursorExecutor: pytest PASSED")
        return True

    def execute_patch(
        self,
        target_file: str,
        cursor_prompt: str,
        raw_error: str,
    ) -> bool:
        """
        Backup → Cursor CLI → require file change → ast.parse → pytest → keep/rollback.
        """
        self.last_error = ""
        self.repo_root = resolve_repo_root()
        try:
            _ensure_cursor_api_key()
        except RuntimeError as exc:
            self.last_error = str(exc)
            logger.error("CursorExecutor: %s", exc)
            return False

        rel = _resolve_target(target_file, self.repo_root)
        abs_path = _abs(self.repo_root, rel)
        if not abs_path.exists():
            self.last_error = f"target file missing: {rel} (root={self.repo_root})"
            logger.error("CursorExecutor: %s", self.last_error)
            return False

        before_mtime = abs_path.stat().st_mtime
        before_text = abs_path.read_text(encoding="utf-8")
        backup = self._create_backup(rel)

        # Prompt paths relative to workspace
        display_path = str(rel)
        message = (
            f"{cursor_prompt}\n\n"
            f"Файл сбоя: {display_path}\n"
            f"Рабочая директория: {self.repo_root}\n"
            f"Правила: минимальный дифф, не трогай .env/секреты, сохрани публичные API.\n"
            f"Лог:\n{raw_error[-3500:]}"
        )
        env = _augment_path(os.environ.copy())
        env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]
        env["CI"] = "1"

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

        after_text = abs_path.read_text(encoding="utf-8")
        after_mtime = abs_path.stat().st_mtime
        if after_text == before_text and after_mtime == before_mtime:
            self.last_error = "agent exited 0 but file unchanged (NO-OP)"
            logger.error("CursorExecutor: %s", self.last_error)
            self._restore(rel, backup)
            return False

        if not self._ast_ok(abs_path):
            logger.error("CursorExecutor: AST failed — rollback (%s)", self.last_error)
            self._restore(rel, backup)
            return False

        if not self._pytest_ok(env):
            logger.error("CursorExecutor: pytest failed — rollback")
            self._restore(rel, backup)
            self._git_checkout(rel)
            return False

        try:
            if backup.exists():
                backup.unlink()
        except OSError:
            pass
        logger.info("CursorExecutor: patch OK for %s", rel)
        return True
