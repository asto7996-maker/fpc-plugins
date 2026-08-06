#!/usr/bin/env bash
#
# Cloud Agent install script for the fpc-plugins repository.
#
# This repo contains FunPay Cardinal plugins (module-level .py files that plug
# into sidor0912/FunPayCardinal). To develop and validate them end-to-end we
# need the host framework so that plugin imports (cardinal, tg_bot, FunPayAPI)
# resolve. This script:
#   1. installs the FunPay Cardinal host into $FPC_HOME (clone or update),
#   2. creates a Python venv and installs the host + plugin dependencies,
#   3. links this repo's plugins into the host's plugins/ directory,
#   4. runs the plugin loader to confirm every plugin loads.
#
# It is idempotent: re-running updates the host and refreshes dependencies.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FPC_HOME="${FPC_HOME:-$HOME/FunPayCardinal}"
FPC_REPO="${FPC_REPO:-https://github.com/sidor0912/FunPayCardinal.git}"
VENV_DIR="$FPC_HOME/.venv"

echo "==> fpc-plugins repo : $REPO_DIR"
echo "==> FunPay Cardinal  : $FPC_HOME"

# --- 1. System dependency: python venv support -------------------------------
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
    echo "==> Installing python3-venv (needs sudo)"
    PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    sudo apt-get update -qq
    sudo apt-get install -y -qq "python${PYVER}-venv" || sudo apt-get install -y -qq python3-venv
fi

# --- 2. Install / update the FunPay Cardinal host ----------------------------
if [ -d "$FPC_HOME/.git" ]; then
    echo "==> Updating existing FunPay Cardinal checkout"
    git -C "$FPC_HOME" pull --ff-only || echo "    (pull skipped; keeping current checkout)"
else
    echo "==> Cloning FunPay Cardinal host"
    git clone --depth 1 "$FPC_REPO" "$FPC_HOME"
fi

# Runtime directories the host normally creates on first boot.
mkdir -p "$FPC_HOME/plugins" "$FPC_HOME/storage/plugins" "$FPC_HOME/configs" "$FPC_HOME/logs"

# --- 3. Python environment ---------------------------------------------------
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "==> Creating virtualenv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
echo "==> Installing FunPay Cardinal dependencies"
"$VENV_DIR/bin/pip" install --quiet -r "$FPC_HOME/requirements.txt"

# --- 4. Link this repo's plugins into the host ------------------------------
echo "==> Linking repo plugins into host plugins/ directory"
# Drop stale symlinks that point back into this repo (renamed/removed plugins).
find "$FPC_HOME/plugins" -maxdepth 1 -type l | while read -r link; do
    target="$(readlink "$link")"
    case "$target" in
        "$REPO_DIR"/*) rm -f "$link" ;;
    esac
done
for f in "$REPO_DIR"/plugins/*.py; do
    ln -sfn "$f" "$FPC_HOME/plugins/$(basename "$f")"
done

# --- 5. Validate that all plugins load through the host ----------------------
echo "==> Validating plugins"
( cd "$FPC_HOME" && FPC_HOME="$FPC_HOME" "$VENV_DIR/bin/python" "$REPO_DIR/.cursor/validate_plugins.py" ) \
    || echo "    (plugin validation reported problems; see output above)"

echo "==> Install complete."
echo "    Activate with: source \"$VENV_DIR/bin/activate\" (host at $FPC_HOME)"
