"""
Cursor CLI self-healer — fully automatic patch loop.

* Loads ``CURSOR_API_KEY`` from ``.env`` into ``os.environ``
* Auto-installs Cursor Agent CLI if missing (``curl https://cursor.com/install``)
* Runs headless: ``agent -p --force --trust --workspace …``
* Verifies with ``pytest tests/test_bot.py``
* Rolls back via file snapshot (and ``git checkout`` when a repo is available)
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

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
ENV_FILE: Path = REPO_ROOT / ".env"
TEST_TARGET = "tests/test_bot.py"
AGENT_TIMEOUT_SEC = 300
INSTALL_TIMEOUT_SEC = 180
BACKUP_DIR: Path = REPO_ROOT / "dwar_bot" / ".heal_backups"
# Written before systemctl restart so LogWatcher skips boot-scan of the
# same error that was just healed (otherwise SUCCESS → restart → re-heal loop).
SKIP_BOOT_MARKER: Path = REPO_ROOT / "dwar_bot" / ".heal_skip_boot"
RESTART_AFTER_HEAL = os.getenv("DWAR_RESTART_AFTER_HEAL", "1") != "0"


def _load_dotenv(path: Optional[Path] = None, *, overwrite: bool = False) -> None:
    """Load KEY=VALUE pairs from .env into os.environ."""
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.extend(
        [
            ENV_FILE,
            REPO_ROOT / "dwar_bot" / ".env",
            Path.cwd() / ".env",
            Path.cwd() / "dwar_bot" / ".env",
        ]
    )
    target = next((p for p in candidates if p.exists()), None)
    if not target:
        return
    try:
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if not key:
                continue
            if overwrite or key not in os.environ or not str(os.environ.get(key, "")).strip():
                os.environ[key] = value
    except OSError as exc:
        logger.warning("Could not read .env (%s): %s", target, exc)


def _ensure_cursor_api_key() -> str:
    _load_dotenv(overwrite=False)
    # Prefer dwar_bot/.env explicitly (VPS layout)
    _load_dotenv(REPO_ROOT / "dwar_bot" / ".env", overwrite=False)
    key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "CURSOR_API_KEY is empty — add it to .env before healing."
        )
    os.environ["CURSOR_API_KEY"] = key
    return key


def _augment_path(env: dict) -> dict:
    """Ensure ~/.local/bin and ~/.cursor/bin are on PATH for the agent binary."""
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


def _which_agent(env: Optional[dict] = None) -> Optional[str]:
    env = _augment_path(dict(env or os.environ))
    for name in ("agent", "cursor-agent", "cursor"):
        path = shutil.which(name, path=env.get("PATH"))
        if path:
            return path
    # Hard paths from official installer
    for p in (
        Path.home() / ".local" / "bin" / "agent",
        Path.home() / ".local" / "bin" / "cursor-agent",
        Path.home() / ".cursor" / "bin" / "agent",
    ):
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def ensure_cursor_cli(force_reinstall: bool = False) -> str:
    """
    Return path to Cursor Agent CLI, installing it automatically if missing.
    """
    env = _augment_path(os.environ.copy())
    if not force_reinstall:
        found = _which_agent(env)
        if found:
            logger.info("Cursor Agent CLI found: %s", found)
            return found

    logger.warning("Cursor Agent CLI missing — installing via cursor.com/install …")
    try:
        result = subprocess.run(
            ["bash", "-lc", "curl -fsS https://cursor.com/install | bash"],
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SEC,
            env=env,
        )
        logger.info(
            "CLI install finished code=%s\n%s",
            result.returncode,
            ((result.stdout or "") + (result.stderr or ""))[-1500:],
        )
    except Exception as exc:
        logger.error("CLI install failed: %s", exc)
        raise RuntimeError(f"Cannot install Cursor Agent CLI: {exc}") from exc

    env = _augment_path(os.environ.copy())
    found = _which_agent(env)
    if not found:
        raise RuntimeError(
            "Cursor Agent CLI install completed but `agent` binary not found on PATH."
        )
    logger.info("Cursor Agent CLI installed: %s", found)
    return found


def _build_prompt(failed_file: str, traceback_text: str) -> str:
    return (
        f"Бот для игры 'Легенда: Наследие Драконов' упал с ошибкой в файле {failed_file}.\n"
        f"Вот стек ошибки:\n"
        f"{traceback_text}\n"
        f"Проанализируй проблему (неверный CSS/XPath селектор, таймаут, логику), "
        f"проверь актуальные данные в config/selectors.py и внеси исправление "
        f"прямо в файл {failed_file}. "
        f"Не спрашивай подтверждений — сразу правь файл и закончи."
    )


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


def _snapshot(rel: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src = _abs(rel)
    stamp = int(time.time())
    dst = BACKUP_DIR / f"{src.name}.{stamp}.bak"
    shutil.copy2(src, dst)
    # Also keep a stable "last" copy for easy restore
    last = BACKUP_DIR / f"{src.name}.last.bak"
    shutil.copy2(src, last)
    return last


def _restore_snapshot(rel: Path, backup: Path) -> None:
    src = _abs(rel)
    if backup.exists():
        shutil.copy2(backup, src)
        logger.warning("Restored %s from snapshot %s", rel, backup)
        return
    # git fallback if available
    if (REPO_ROOT / ".git").exists():
        subprocess.run(
            ["git", "checkout", "--", str(rel)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.warning("Restored %s via git checkout.", rel)


def _run_pytest(env: dict) -> bool:
    env = dict(env)
    # Ensure package imports work on VPS layout (WorkingDirectory=/root)
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

    logger.info("Running verification: %s", " ".join(cmd))
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
        logger.error("pytest timed out.")
        return False
    except Exception as exc:
        logger.error("pytest failed to start: %s", exc)
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


def mark_skip_boot_scan(reason: str = "post-heal") -> None:
    """Tell the next LogWatcher start to seek EOF without re-healing old errors."""
    try:
        SKIP_BOOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SKIP_BOOT_MARKER.write_text(
            f"{time.time()}\n{reason}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write skip-boot marker: %s", exc)


def consume_skip_boot_scan(max_age_sec: int = 600) -> bool:
    """Return True (and delete marker) if a fresh post-heal skip is active."""
    try:
        if not SKIP_BOOT_MARKER.exists():
            return False
        raw = SKIP_BOOT_MARKER.read_text(encoding="utf-8").strip().splitlines()
        SKIP_BOOT_MARKER.unlink(missing_ok=True)
        if not raw:
            return True
        age = time.time() - float(raw[0])
        return age <= max_age_sec
    except (OSError, ValueError) as exc:
        logger.debug("skip-boot marker read failed: %s", exc)
        try:
            SKIP_BOOT_MARKER.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _restart_service() -> None:
    """Reload bot process so patched code is actually used."""
    if not RESTART_AFTER_HEAL:
        return
    mark_skip_boot_scan("post-heal-restart")
    logger.warning("Scheduling service restart to load healed code…")
    try:
        # Detach so restart isn't killed mid-flight with us
        subprocess.Popen(
            ["systemctl", "restart", "dwar_bot.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        logger.error("Could not restart service: %s", exc)


def _run_agent(agent_bin: str, prompt: str, env: dict) -> subprocess.CompletedProcess:
    """
    Prefer modern headless CLI; fall back to legacy ``cursor agent --message``.
    """
    workspace = str(REPO_ROOT)
    attempts = [
        # Official headless form (applies edits with --force, no prompts with --trust)
        [
            agent_bin, "-p", "--force", "--trust",
            "--workspace", workspace,
            prompt,
        ],
        # Alternate binary name / older flags
        [
            agent_bin, "--print", "--force", "--trust",
            "--workspace", workspace,
            prompt,
        ],
        # User-requested shape (if `cursor` wrapper exists)
        ["cursor", "agent", "--message", prompt],
    ]

    last: Optional[subprocess.CompletedProcess] = None
    for cmd in attempts:
        # Skip cursor-wrapper attempt if binary isn't cursor
        if cmd[0] == "cursor" and shutil.which("cursor", path=env.get("PATH")) is None:
            continue
        logger.info("Invoking: %s …", " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""))
        try:
            last = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=AGENT_TIMEOUT_SEC,
                cwd=workspace,
            )
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            logger.error("Agent timed out on: %s", cmd[0])
            raise
        if last.returncode == 0:
            return last
        logger.warning(
            "Agent attempt exit=%s stderr=%s",
            last.returncode,
            (last.stderr or "")[:400],
        )
    if last is None:
        raise FileNotFoundError("No usable Cursor agent command")
    return last


def patch_code_with_cursor(failed_file: str, traceback_text: str) -> bool:
    """
    Fully automatic heal: ensure CLI → snapshot → agent patch → pytest → restore on fail.
    """
    rel = _resolve_failed_path(failed_file)
    abs_path = _abs(rel)
    if not abs_path.exists():
        logger.error("Failed file does not exist: %s", abs_path)
        return False

    try:
        _ensure_cursor_api_key()
        agent_bin = ensure_cursor_cli()
    except Exception as exc:
        logger.error("Self-healer bootstrap failed: %s", exc)
        return False

    prompt = _build_prompt(str(rel), traceback_text)
    env = _augment_path(os.environ.copy())
    env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]
    env["CI"] = "1"  # hint non-interactive

    backup = _snapshot(rel)
    logger.info(
        "Self-heal start: file=%s timeout=%ds agent=%s",
        rel, AGENT_TIMEOUT_SEC, agent_bin,
    )

    try:
        result = _run_agent(agent_bin, prompt, env)
    except subprocess.TimeoutExpired:
        logger.error("Cursor agent timed out — restoring snapshot.")
        _restore_snapshot(rel, backup)
        return False
    except Exception as exc:
        logger.exception("Cursor agent invocation failed: %s", exc)
        _restore_snapshot(rel, backup)
        return False

    logger.info(
        "Cursor agent finished code=%s\n%s",
        result.returncode,
        ((result.stdout or "") + "\n" + (result.stderr or ""))[-2000:],
    )

    if result.returncode != 0:
        _restore_snapshot(rel, backup)
        return False

    if _run_pytest(env):
        logger.info("Self-heal SUCCESS for %s", rel)
        _restart_service()
        return True

    _restore_snapshot(rel, backup)
    return False
