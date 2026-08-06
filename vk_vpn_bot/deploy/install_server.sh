#!/usr/bin/env bash
# Установка VK-бота на сервер: venv + systemd (24/7).
set -euo pipefail

ROOT="${1:-/root/vk_vpn_bot}"
SERVICE_NAME="vk-vpn-bot"

echo "==> Install dir: $ROOT"
mkdir -p "$ROOT/data" "$ROOT/logs"

if [[ ! -f "$ROOT/.env" ]]; then
  echo "ERROR: $ROOT/.env не найден. Скопируйте секреты перед установкой."
  exit 1
fi

echo "==> Python venv"
python3 -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install --upgrade pip
pip install -r "$ROOT/requirements.txt"

echo "==> systemd unit"
install -m 644 "$ROOT/deploy/vk-vpn-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"
sleep 2
systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
echo "==> Done. Logs: journalctl -u ${SERVICE_NAME} -f"
