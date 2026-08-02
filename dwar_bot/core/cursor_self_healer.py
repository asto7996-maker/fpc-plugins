"""
Cursor CLI self-healer — fully automatic patch loop (zero human steps).

Pipeline
--------
1. Load ``CURSOR_API_KEY`` from ``.env`` (VPS: ``/root/dwar_bot/.env``)
2. Auto-install Cursor Agent CLI if missing
3. Snapshot target file(s)
4. Headless ``agent -p --force --trust`` patch
5. Require real file change (hash) — no-op ≠ success
6. ``pytest tests/test_bot.py`` gate
7. Verified ``systemctl restart dwar_bot.service`` (+ skip-boot marker)
8. Rollback snapshot on any failure
"""

from __future__ import annotations

import hashlib
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
SERVICE_NAME = os.getenv("DWAR_SERVICE_NAME", "dwar_bot.service")


def restart_after_heal_enabled() -> bool:
    """Read at call-time so .env loaded in main() is honored."""
    return os.getenv("DWAR_RESTART_AFTER_HEAL", "1") != "0"


def _load_dotenv(path: Optional[Path] = None, *, overwrite: bool = False) -> None:
    """Load KEY=VALUE pairs from .env into os.environ."""
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.extend(
        [
            ENV_FILE,
            REPO_ROOT / "dwar_bot" / ".env",
            Path("/root/dwar_bot/.env"),
            Path("/root/.env"),
            Path.cwd() / ".env",
            Path.cwd() / "dwar_bot" / ".env",
        ]
    )
    seen: set[str] = set()
    for target in candidates:
        try:
            key = str(target.resolve())
        except OSError:
            key = str(target)
        if key in seen or not target.exists():
            continue
        seen.add(key)
        try:
            for raw in target.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, value = line.partition("=")
                k = k.strip()
                value = value.strip().strip("'").strip('"')
                if not k:
                    continue
                if overwrite or k not in os.environ or not str(os.environ.get(k, "")).strip():
                    os.environ[k] = value
        except OSError as exc:
            logger.warning("Could not read .env (%s): %s", target, exc)


def _ensure_cursor_api_key() -> str:
    _load_dotenv(overwrite=False)
    _load_dotenv(REPO_ROOT / "dwar_bot" / ".env", overwrite=False)
    _load_dotenv(Path("/root/dwar_bot/.env"), overwrite=False)
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
    for p in (
        Path.home() / ".local" / "bin" / "agent",
        Path.home() / ".local" / "bin" / "cursor-agent",
        Path.home() / ".cursor" / "bin" / "agent",
    ):
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def ensure_cursor_cli(force_reinstall: bool = False) -> str:
    """Return path to Cursor Agent CLI, installing it automatically if missing."""
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


def heal_ready() -> tuple[bool, str]:
    """Return (ok, detail) — whether full-auto heal can run right now."""
    try:
        _ensure_cursor_api_key()
    except Exception as exc:
        return False, f"API key: {exc}"
    try:
        path = ensure_cursor_cli()
    except Exception as exc:
        return False, f"CLI: {exc}"
    return True, f"ready agent={path}"


def _build_prompt(failed_file: str, traceback_text: str) -> str:
    return (
        f"Бот для игры 'Легенда: Наследие Драконов' упал с ошибкой в файле {failed_file}.\n"
        f"Вот стек ошибки:\n"
        f"{traceback_text}\n\n"
        f"ЗАДАЧА (полностью автоматически, без вопросов):\n"
        f"1. Найди корневую причину по traceback.\n"
        f"2. Исправь код. Можно править связанные файлы в dwar_bot/ и config/selectors.py "
        f"(не только {failed_file}), если это нужно для фикса.\n"
        f"3. Минимальный дифф. Не рефактори и не трогай .env / секреты.\n"
        f"4. Сохрани изменения на диск и закончи.\n"
        f"Не спрашивай подтверждений — сразу правь файлы."
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


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    try:
        h.update(path.read_bytes())
    except OSError:
        return ""
    return h.hexdigest()


def _snapshot_tree() -> dict[str, str]:
    """Hash all .py under dwar_bot/ + config/selectors.py for change detection."""
    out: dict[str, str] = {}
    roots = [REPO_ROOT / "dwar_bot", REPO_ROOT / "config"]
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if ".heal_backups" in p.parts or "__pycache__" in p.parts:
                continue
            try:
                rel = str(p.relative_to(REPO_ROOT))
            except ValueError:
                rel = str(p)
            out[rel] = _file_hash(p)
    return out


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed = []
    for rel, h in after.items():
        if before.get(rel) != h:
            changed.append(rel)
    for rel in before:
        if rel not in after:
            changed.append(rel)
    return sorted(changed)


def _snapshot(rel: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src = _abs(rel)
    stamp = int(time.time())
    dst = BACKUP_DIR / f"{src.name}.{stamp}.bak"
    shutil.copy2(src, dst)
    last = BACKUP_DIR / f"{src.name}.last.bak"
    shutil.copy2(src, last)
    return last


def _restore_snapshot(rel: Path, backup: Path) -> None:
    src = _abs(rel)
    if backup.exists():
        shutil.copy2(backup, src)
        logger.warning("Restored %s from snapshot %s", rel, backup)
        return
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


def _restart_service() -> bool:
    """
    Reload bot process so patched code is actually used.

    Must be detached: calling ``systemctl restart`` synchronously from inside
    the unit kills us mid-call and looks like a failure. Schedule a delayed
    restart in a new session instead.
    """
    if not restart_after_heal_enabled():
        logger.warning("DWAR_RESTART_AFTER_HEAL=0 — skipping service restart.")
        return False

    mark_skip_boot_scan("post-heal-restart")
    logger.warning("Scheduling detached service restart to load healed code…")

    # Delayed restart outside our cgroup/session so we survive until stop arrives
    script = (
        f"sleep 3; "
        f"systemctl restart {SERVICE_NAME} "
        f"|| systemctl kill -s SIGTERM {SERVICE_NAME} "
        f"|| kill -TERM {os.getpid()} || true"
    )
    try:
        subprocess.Popen(
            ["bash", "-lc", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Detached restart scheduled for %s", SERVICE_NAME)
        return True
    except Exception as exc:
        logger.error("Detached restart failed: %s — trying self-kill", exc)

    try:
        pid = os.getpid()
        subprocess.Popen(
            ["bash", "-lc", f"sleep 2; kill -TERM {pid} || true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:
        logger.error("Self-restart fallback failed: %s", exc)
        return False


def _run_agent(agent_bin: str, prompt: str, env: dict) -> subprocess.CompletedProcess:
    """Prefer modern headless CLI; fall back to legacy ``cursor agent --message``."""
    workspace = str(REPO_ROOT)
    attempts = [
        [
            agent_bin, "-p", "--force", "--trust",
            "--workspace", workspace,
            prompt,
        ],
        [
            agent_bin, "--print", "--force", "--trust",
            "--workspace", workspace,
            prompt,
        ],
        ["cursor", "agent", "--message", prompt],
    ]

    last: Optional[subprocess.CompletedProcess] = None
    for cmd in attempts:
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
    Fully automatic heal:
    ensure CLI → snapshot → agent patch → require file change → pytest → restart.
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
    env["CI"] = "1"

    backup = _snapshot(rel)
    before = _snapshot_tree()
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

    after = _snapshot_tree()
    changed = _changed_files(before, after)
    if not changed:
        logger.error(
            "Self-heal NO-OP: agent exited 0 but no .py files changed — not SUCCESS."
        )
        _restore_snapshot(rel, backup)
        return False
    logger.info("Self-heal changed files: %s", ", ".join(changed[:12]))

    if not _run_pytest(env):
        # Restore primary target; other changed files may remain — best effort
        _restore_snapshot(rel, backup)
        return False

    logger.info("Self-heal SUCCESS for %s (changed=%d)", rel, len(changed))
    restarted = _restart_service()
    if not restarted:
        logger.error(
            "Patch applied + pytest OK but restart FAILED — "
            "process may still run old code until manual restart."
        )
        # Still return True: code on disk is fixed; systemd Restart= or next
        # deploy will pick it up. Caller may notify.
    return True
