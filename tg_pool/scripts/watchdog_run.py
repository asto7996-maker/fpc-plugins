#!/usr/bin/env python3
"""
Process supervisor for tg_pool.

Restarts the bot when it exits OR when the in-process heartbeat file goes stale
(event-loop freeze / hung Telethon / blocked polling).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
HEARTBEAT = Path(os.environ.get("TG_POOL_HEARTBEAT_FILE", "/tmp/tg_pool_heartbeat"))
LOG_PATH = Path(os.environ.get("TG_POOL_LOG", "/tmp/tg_pool_bot.log"))
MAX_STALE_SEC = float(os.environ.get("TG_POOL_HEARTBEAT_STALE_SEC", "90"))
POLL_SEC = float(os.environ.get("TG_POOL_WATCHDOG_POLL_SEC", "10"))
RESTART_DELAY_SEC = float(os.environ.get("TG_POOL_RESTART_DELAY_SEC", "3"))


def _python() -> str:
    if VENV_PYTHON.exists():
        return str(VENV_PYTHON)
    return sys.executable


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO)
    env["TG_POOL_HEARTBEAT_FILE"] = str(HEARTBEAT)
    # Avoid buffering so watchdog log stays live
    env["PYTHONUNBUFFERED"] = "1"
    return env


def main() -> int:
    python = _python()
    print(f"[watchdog] python={python}", flush=True)
    print(f"[watchdog] heartbeat={HEARTBEAT} stale>{MAX_STALE_SEC}s", flush=True)
    print(f"[watchdog] log={LOG_PATH}", flush=True)

    stopping = False

    def _stop(signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        print(f"[watchdog] signal {signum} — stopping", flush=True)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not stopping:
        if HEARTBEAT.exists():
            try:
                HEARTBEAT.unlink()
            except OSError:
                pass

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"\n----- watchdog spawn {time.strftime('%Y-%m-%d %H:%M:%S')} -----\n")
            log.flush()
            proc = subprocess.Popen(
                [python, "-m", "tg_pool"],
                cwd=str(REPO),
                env=_env(),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print(f"[watchdog] started pid={proc.pid}", flush=True)

        killed_stale = False
        while not stopping:
            code = proc.poll()
            if code is not None:
                print(f"[watchdog] process exited code={code}", flush=True)
                break

            if HEARTBEAT.exists():
                age = time.time() - HEARTBEAT.stat().st_mtime
                if age > MAX_STALE_SEC:
                    print(
                        f"[watchdog] heartbeat stale ({age:.0f}s) — killing pid={proc.pid}",
                        flush=True,
                    )
                    killed_stale = True
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait(timeout=5)
                    break
            time.sleep(POLL_SEC)

        if stopping:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            break

        reason = "stale-heartbeat" if killed_stale else "exit"
        print(f"[watchdog] restart in {RESTART_DELAY_SEC}s ({reason})", flush=True)
        time.sleep(RESTART_DELAY_SEC)

    print("[watchdog] bye", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
