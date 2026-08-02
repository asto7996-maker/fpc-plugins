"""
Cursor CLI self-healer — patches failed bot modules via ``cursor agent``.

Reads ``CURSOR_API_KEY`` from ``.env`` (or the process environment), injects it
into the subprocess env, runs the agent against the failed file, then verifies
with ``pytest tests/test_bot.py``. On failure rolls back with ``git checkout``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Repo root: dwar_bot/core/ → parents[2] == workspace
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ENV_FILE: Path = REPO_ROOT / ".env"
TEST_TARGET = "tests/test_bot.py"
AGENT_TIMEOUT_SEC = 120


def _load_dotenv(path: Optional[Path] = None) -> None:
    """Load KEY=VALUE pairs from .env into os.environ (no overwrite of set vars)."""
    target = Path(path) if path else ENV_FILE
    if not target.exists():
        # Also try dwar_bot/.env / cwd
        for alt in (REPO_ROOT / "dwar_bot" / ".env", Path.cwd() / ".env"):
            if alt.exists():
                target = alt
                break
        else:
            return
    try:
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        logger.warning("Could not read .env (%s): %s", target, exc)


def _ensure_cursor_api_key() -> str:
    _load_dotenv()
    key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "CURSOR_API_KEY is empty — add it to .env or export it before healing."
        )
    os.environ["CURSOR_API_KEY"] = key
    return key


def _build_prompt(failed_file: str, traceback_text: str) -> str:
    return (
        f"Бот для игры 'Легенда: Наследие Драконов' упал с ошибкой в файле {failed_file}.\n"
        f"Вот стек ошибки:\n"
        f"{traceback_text}\n"
        f"Проанализируй проблему (неверный CSS/XPath селектор, таймаут, логику), "
        f"проверь актуальные данные в config/selectors.py и внеси исправление "
        f"прямо в файл {failed_file}."
    )


def _resolve_failed_path(failed_file: str) -> Path:
    """Return path relative to repo root when possible (for git checkout)."""
    p = Path(failed_file)
    if p.is_absolute():
        try:
            return p.relative_to(REPO_ROOT)
        except ValueError:
            return p
    # Prefer dwar_bot/… if bare modules/… was extracted from traceback
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


def _run_pytest() -> bool:
    cmd = ["pytest", TEST_TARGET, "-q", "--tb=short"]
    logger.info("Running verification: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=os.environ.copy(),
        )
    except FileNotFoundError:
        # pytest not on PATH — try python -m pytest
        result = subprocess.run(
            ["python3", "-m", "pytest", TEST_TARGET, "-q", "--tb=short"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        logger.error("pytest timed out.")
        return False

    if result.returncode == 0:
        logger.info("pytest OK.")
        return True
    logger.error(
        "pytest FAILED (code=%s)\nstdout:\n%s\nstderr:\n%s",
        result.returncode,
        (result.stdout or "")[-1500:],
        (result.stderr or "")[-1500:],
    )
    return False


def _git_checkout(rel_path: Path) -> None:
    logger.warning("Rolling back %s via git checkout.", rel_path)
    subprocess.run(
        ["git", "checkout", "--", str(rel_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def patch_code_with_cursor(failed_file: str, traceback_text: str) -> bool:
    """
    Ask Cursor CLI agent to patch ``failed_file``, then run pytest.

    Returns True if tests pass after the agent run; otherwise rolls back the
    file with ``git checkout -- {failed_file}`` and returns False.
    """
    rel = _resolve_failed_path(failed_file)
    abs_path = (REPO_ROOT / rel) if not Path(rel).is_absolute() else Path(rel)
    if not abs_path.exists():
        logger.error("Failed file does not exist: %s", abs_path)
        return False

    try:
        _ensure_cursor_api_key()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return False

    prompt = _build_prompt(str(rel), traceback_text)
    env = os.environ.copy()
    # Explicitly inject key into the child process environment
    env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]

    cmd = ["cursor", "agent", "--message", prompt]
    logger.info(
        "Invoking Cursor agent for %s (timeout=%ds)…",
        rel, AGENT_TIMEOUT_SEC,
    )
    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SEC,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        logger.error("Cursor agent timed out after %ds — rolling back.", AGENT_TIMEOUT_SEC)
        _git_checkout(rel)
        return False
    except FileNotFoundError:
        logger.error(
            "`cursor` CLI not found on PATH — cannot self-heal. "
            "Install Cursor CLI or leave the bot paused."
        )
        return False
    except Exception as exc:
        logger.exception("Cursor agent invocation failed: %s", exc)
        _git_checkout(rel)
        return False

    if result.returncode != 0:
        logger.error(
            "Cursor agent exited %s\nstdout:\n%s\nstderr:\n%s",
            result.returncode,
            (result.stdout or "")[-2000:],
            (result.stderr or "")[-2000:],
        )
        _git_checkout(rel)
        return False

    logger.info(
        "Cursor agent finished.\n%s",
        (result.stdout or "")[-1000:] or "(no stdout)",
    )

    if _run_pytest():
        return True

    _git_checkout(rel)
    return False
