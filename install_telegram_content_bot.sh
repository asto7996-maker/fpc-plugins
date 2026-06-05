#!/usr/bin/env bash
# Install/update the safe Telegram Content Machine bot on Ubuntu/Debian.
#
# Usage:
#   bash install_telegram_content_bot.sh
#   APP_DIR=/opt/my-content-bot SERVICE_NAME=my-content-bot bash install_telegram_content_bot.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/telegram-content-machine}"
SERVICE_NAME="${SERVICE_NAME:-telegram-content-machine}"
SERVICE_USER="${SERVICE_USER:-tgbot}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Telegram Content Machine installer ==="
echo "App dir:      ${APP_DIR}"
echo "Service:      ${SERVICE_NAME}.service"
echo "Service user: ${SERVICE_USER}"

if [[ ! -f "${REPO_DIR}/telegram_content_machine_bot.py" ]]; then
    echo "ERROR: telegram_content_machine_bot.py not found in ${REPO_DIR}" >&2
    exit 1
fi

echo "Installing system packages..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Creating system user ${SERVICE_USER}..."
    sudo useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "Creating application directories..."
sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}"
sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}/data"
sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}/data/tmp"

echo "Copying application files..."
sudo install -m 0644 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${REPO_DIR}/telegram_content_machine_bot.py" \
    "${APP_DIR}/telegram_content_machine_bot.py"
sudo install -m 0644 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${REPO_DIR}/requirements-telegram-bot.txt" \
    "${APP_DIR}/requirements-telegram-bot.txt"

if [[ ! -f "${APP_DIR}/.env" ]]; then
    echo "Creating ${APP_DIR}/.env from example..."
    sudo install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
        "${REPO_DIR}/.env.telegram-content.example" \
        "${APP_DIR}/.env"
else
    echo "Existing ${APP_DIR}/.env preserved."
fi

echo "Creating Python virtual environment..."
if [[ ! -d "${APP_DIR}/venv" ]]; then
    sudo -u "${SERVICE_USER}" python3 -m venv "${APP_DIR}/venv"
fi
sudo -u "${SERVICE_USER}" "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip setuptools wheel
sudo -u "${SERVICE_USER}" "${APP_DIR}/venv/bin/pip" install --upgrade -r "${APP_DIR}/requirements-telegram-bot.txt"

echo "Writing systemd service..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<SERVICE
[Unit]
Description=Safe Telegram Content Machine Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/telegram_content_machine_bot.py --env-file ${APP_DIR}/.env
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=${APP_DIR}

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"

echo ""
echo "=== Installed ==="
echo "1) Edit config:"
echo "   sudo nano ${APP_DIR}/.env"
echo ""
echo "2) Validate config:"
echo "   sudo -u ${SERVICE_USER} ${APP_DIR}/venv/bin/python ${APP_DIR}/telegram_content_machine_bot.py --env-file ${APP_DIR}/.env --check-config"
echo ""
echo "3) Start service:"
echo "   sudo systemctl restart ${SERVICE_NAME}.service"
echo "   sudo systemctl status ${SERVICE_NAME}.service --no-pager"
echo ""
echo "4) Logs:"
echo "   sudo journalctl -u ${SERVICE_NAME}.service -f"
echo ""
echo "Important: add the bot as admin to the target channel and to every allowed source channel."
