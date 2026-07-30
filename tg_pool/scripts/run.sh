#!/usr/bin/env bash
# Launch tg_pool under the heartbeat watchdog (venv).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
VENV="$ROOT/.venv"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Creating venv at $VENV ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -U pip
  "$VENV/bin/pip" install -r "$ROOT/requirements.txt" aiosqlite
fi

export PYTHONPATH="$REPO"
export PYTHONUNBUFFERED=1
export TG_POOL_HEARTBEAT_FILE="${TG_POOL_HEARTBEAT_FILE:-/tmp/tg_pool_heartbeat}"
export TG_POOL_LOG="${TG_POOL_LOG:-/tmp/tg_pool_bot.log}"
export TG_POOL_HEARTBEAT_STALE_SEC="${TG_POOL_HEARTBEAT_STALE_SEC:-90}"

exec "$VENV/bin/python" "$ROOT/scripts/watchdog_run.py"
