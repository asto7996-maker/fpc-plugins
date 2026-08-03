"""
Isolated Cursor CLI engine with backup rotation and AST/pytest gates.

Pipeline
--------
1. Load ``CURSOR_API_KEY`` from ``.env`` into ``os.environ``
2. Snapshot ``failed_file`` → ``data/backups/{name}.bak_{timestamp}``
3. Invoke Cursor agent with an extended Playwright/Python prompt
4. Validate patched source via ``ast_checker``
5. Run ``pytest tests/test_bot.py``
6. Keep patch + drop backup on success; rollback on any failure
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from dwar_bot.core.self_healing.ast_checker import validate_python_code

logger = logging.getLogger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
BACKUP_ROOT: Path = REPO_ROOT / "data" / "backups"
TEST_TARGET = "tests/test_bot.py"
AGENT_TIMEOUT_SEC = 180


def _load_dotenv() -> None:
    """Best-effort KEY=VALUE load from known .env locations."""
    candidates = [
        REPO_ROOT / ".env",
        REPO_ROOT / "dwar_bot" / ".env",
        Path("/root/dwar_bot/.env"),
        Path("/root/.env"),
        Path.cwd() / ".env",
    ]
    try:
        from dwar_bot.core.cursor_self_healer import _load_dotenv as _shared_load

        _shared_load(overwrite=False)
        return
    except Exception:
        pass

    for target in candidates:
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
                if key and (key not in os.environ or not str(os.environ.get(key, "")).strip()):
                    os.environ[key] = value
        except OSError as exc:
            logger.warning("cursor_engine: cannot read %s: %s", target, exc)


def _ensure_api_key() -> str:
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


def _resolve_failed_path(failed_file: str) -> Path:
    p = Path(failed_file)
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


def _backup_slug(rel: Path) -> str:
    """Flatten nested path for backup filename."""
    return str(rel).replace("\\", "/").replace("/", "__")


def _create_backup(rel: Path) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    src = _abs(rel)
    stamp = int(time.time())
    dst = BACKUP_ROOT / f"{_backup_slug(rel)}.bak_{stamp}"
    shutil.copy2(src, dst)
    logger.info("Backup created: %s → %s", rel, dst)
    return dst


def _restore_from_backup(rel: Path, backup: Path) -> None:
    src = _abs(rel)
    if backup.exists():
        shutil.copy2(backup, src)
        logger.warning("Rolled back %s from backup %s", rel, backup)
        return
    if (REPO_ROOT / ".git").exists():
        subprocess.run(
            ["git", "checkout", "--", str(rel)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.warning("Rolled back %s via git checkout", rel)


def _delete_backup(backup: Path) -> None:
    try:
        if backup.exists():
            backup.unlink()
            logger.info("Backup removed after successful heal: %s", backup)
    except OSError as exc:
        logger.warning("Could not remove backup %s: %s", backup, exc)


def _build_prompt(
    failed_file: str,
    traceback_text: str,
    *,
    dom_snapshot_path: Optional[str] = None,
) -> str:
    extra_dom = ""
    if dom_snapshot_path:
        extra_dom = (
            f"\n     - DOM snapshot path: {dom_snapshot_path} "
            f"(используй для диагностики селекторов / iframe).\n"
        )
    return (
        "Ты — ведущий инженер по Playwright и Python. Бот для игры "
        "'Легенды: Наследие Драконов' вылетел с ошибкой.\n"
        f"Файл сбоя: {failed_file}\n"
        f"Traceback:\n{traceback_text}\n\n"
        "ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ:\n"
        "     - Проверь валидность селекторов в config/selectors.py.\n"
        "     - Проверь логику задержек (HumanBehavior) и обработку iFrames (main_frame).\n"
        "     - Учти HTTP/WS игровой клиент (game_client, hunt_farm, quest answer) "
        "если traceback указывает на протокольный слой.\n"
        f"{extra_dom}"
        f"Исправь ошибку непосредственно в {failed_file}. "
        "Не меняй сигнатуры публичных методов!"
    )


def _run_cursor_cli(prompt: str, env: dict) -> subprocess.CompletedProcess:
    """
    Prefer the requested ``cursor agent --message`` invocation; fall back to
    headless ``agent -p`` when the desktop-style CLI is unavailable.
    """
    workspace = str(REPO_ROOT)
    attempts = [
        ["cursor", "agent", "--message", prompt],
        ["agent", "-p", "--force", "--trust", "--workspace", workspace, prompt],
        ["cursor-agent", "-p", "--force", "--trust", "--workspace", workspace, prompt],
    ]
    last: Optional[subprocess.CompletedProcess] = None
    for cmd in attempts:
        bin_name = cmd[0]
        if shutil.which(bin_name, path=env.get("PATH")) is None:
            continue
        logger.info("cursor_engine: invoking %s …", bin_name)
        try:
            last = subprocess.run(
                cmd,
                env=env,
                timeout=AGENT_TIMEOUT_SEC,
                capture_output=True,
                text=True,
                cwd=workspace,
            )
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            logger.error("cursor_engine: CLI timed out (%ss)", AGENT_TIMEOUT_SEC)
            raise
        if last.returncode == 0:
            return last
        logger.warning(
            "cursor_engine: %s exit=%s stderr=%s",
            bin_name,
            last.returncode,
            (last.stderr or "")[:400],
        )
    if last is None:
        raise FileNotFoundError(
            "Cursor CLI not found (tried: cursor, agent, cursor-agent)"
        )
    return last


def _validate_file_ast(path: Path) -> tuple[bool, Optional[str]]:
    try:
        code = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"не удалось прочитать {path}: {exc}"
    return validate_python_code(code)


def _run_pytest(env: dict) -> bool:
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

    logger.info("cursor_engine: running %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.error("cursor_engine: pytest timed out")
        return False
    except Exception as exc:
        logger.error("cursor_engine: pytest failed to start: %s", exc)
        return False

    if result.returncode == 0:
        logger.info("cursor_engine: pytest PASSED")
        return True
    logger.error(
        "cursor_engine: pytest FAILED code=%s\n%s\n%s",
        result.returncode,
        (result.stdout or "")[-1200:],
        (result.stderr or "")[-1200:],
    )
    return False


def apply_patch_via_cursor(
    failed_file: str,
    traceback_text: str,
    dom_snapshot_path: Optional[str] = None,
) -> bool:
    """
    Apply an automated Cursor CLI patch with AST + pytest validation.

    Returns ``True`` only when the patch is applied, AST-clean, and tests pass.
    On any failure the target file is restored from the backup (or git).
    """
    rel = _resolve_failed_path(failed_file)
    abs_path = _abs(rel)
    if not abs_path.exists():
        logger.error("cursor_engine: failed file does not exist: %s", abs_path)
        return False

    try:
        _ensure_api_key()
    except Exception as exc:
        logger.error("cursor_engine: API key bootstrap failed: %s", exc)
        return False

    # Ensure CLI is present when possible (auto-install via legacy helper).
    try:
        from dwar_bot.core.cursor_self_healer import ensure_cursor_cli

        ensure_cursor_cli()
    except Exception as exc:
        logger.warning("cursor_engine: CLI ensure skipped/failed: %s", exc)

    prompt = _build_prompt(
        str(rel),
        traceback_text,
        dom_snapshot_path=dom_snapshot_path,
    )
    env = _augment_path(os.environ.copy())
    env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]
    env["CI"] = "1"

    backup = _create_backup(rel)
    logger.info(
        "cursor_engine: start file=%s timeout=%ds",
        rel,
        AGENT_TIMEOUT_SEC,
    )

    try:
        result = _run_cursor_cli(prompt, env)
    except subprocess.TimeoutExpired:
        _restore_from_backup(rel, backup)
        return False
    except Exception as exc:
        logger.exception("cursor_engine: CLI invocation failed: %s", exc)
        _restore_from_backup(rel, backup)
        return False

    logger.info(
        "cursor_engine: CLI finished code=%s\n%s",
        result.returncode,
        ((result.stdout or "") + "\n" + (result.stderr or ""))[-2000:],
    )
    if result.returncode != 0:
        _restore_from_backup(rel, backup)
        return False

    # AST gate — immediate rollback on failure
    ok_ast, ast_err = _validate_file_ast(abs_path)
    if not ok_ast:
        logger.error("cursor_engine: AST validation failed: %s — rollback", ast_err)
        _restore_from_backup(rel, backup)
        return False

    # Also validate any other changed .py under dwar_bot/ that we can cheaply check
    # (primary file is mandatory; siblings are best-effort).
    if not _run_pytest(env):
        _restore_from_backup(rel, backup)
        # Prefer git checkout as secondary rollback if backup already applied
        if (REPO_ROOT / ".git").exists():
            subprocess.run(
                ["git", "checkout", "--", str(rel)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
        return False

    _delete_backup(backup)
    logger.info("cursor_engine: SUCCESS for %s", rel)

    # Best-effort service reload so patched code is loaded (detached).
    try:
        from dwar_bot.core.cursor_self_healer import _restart_service

        _restart_service()
    except Exception as exc:
        logger.warning("cursor_engine: post-heal restart skipped: %s", exc)

    return True
