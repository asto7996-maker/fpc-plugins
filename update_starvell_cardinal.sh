#!/usr/bin/env bash
# Обновление Starvell Cardinal до последней версии с GitHub.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/starvell-cardinal}"
REPO_URL="${REPO_URL:-https://github.com/asto7996-maker/fpc-plugins.git}"
REPO_BRANCH="${REPO_BRANCH:-cursor/fpc-parity-280c}"

echo "=== Обновление Starvell Cardinal ==="

if [ "$EUID" -ne 0 ]; then
  echo "Запустите: sudo bash update_starvell_cardinal.sh"
  exit 1
fi

systemctl stop starvell-cardinal.service 2>/dev/null || true

TMP="$(mktemp -d)"
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TMP"

if [ ! -f "$TMP/main.py" ]; then
  REPO_BRANCH="cursor/starvell-cardinal-bot-280c"
  rm -rf "$TMP"
  TMP="$(mktemp -d)"
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TMP"
fi

mkdir -p "$INSTALL_DIR"
rsync -a \
  --exclude='.git' --exclude='venv' --exclude='__pycache__' \
  --exclude='storage' --exclude='logs' --exclude='config/settings.json' \
  "$TMP/" "$INSTALL_DIR/"
rm -rf "$TMP"

if [ -f "$INSTALL_DIR/venv/bin/pip" ]; then
  "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
fi

systemctl daemon-reload
systemctl start starvell-cardinal.service
sleep 2
systemctl status starvell-cardinal.service --no-pager || true
echo "Готово. Проверьте бота: /menu"
