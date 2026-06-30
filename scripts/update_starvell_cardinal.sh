#!/usr/bin/env bash
# Обновление Starvell Cardinal до последней версии с GitHub.
# Использование:
#   curl -fsSL .../scripts/update_starvell_cardinal.sh | sudo bash -s -- cursor/parser-auto-create-6ec3
#   REPO_BRANCH=cursor/parser-auto-create-6ec3 sudo bash update_starvell_cardinal.sh

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/starvell-cardinal}"
SERVICE_USER="${SERVICE_USER:-starvell}"
REPO_URL="${REPO_URL:-https://github.com/asto7996-maker/fpc-plugins.git}"
REPO_BRANCH="${REPO_BRANCH:-${1:-cursor/parser-auto-create-6ec3}}"

echo "=== Обновление Starvell Cardinal (ветка: $REPO_BRANCH) ==="
COMMIT_HINT="$(git ls-remote "$REPO_URL" "refs/heads/$REPO_BRANCH" 2>/dev/null | awk '{print $1}' | head -1)"
if [ -n "$COMMIT_HINT" ]; then
  echo "Коммит на GitHub: ${COMMIT_HINT:0:12}"
fi

if [ "$EUID" -ne 0 ]; then
  echo "Запустите: sudo bash update_starvell_cardinal.sh [ветка]"
  exit 1
fi

id "$SERVICE_USER" &>/dev/null || useradd -r -m -s /bin/bash "$SERVICE_USER"
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "ОШИБКА: python3 не найден. Установите: apt install -y python3 python3-venv python3-pip"
  exit 1
fi

systemctl stop starvell-cardinal.service 2>/dev/null || true

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TMP"

if [ ! -f "$TMP/main.py" ]; then
  echo "Ветка $REPO_BRANCH без main.py, пробую cursor/fix-bot-hang-6ec3 …"
  REPO_BRANCH="cursor/fix-bot-hang-6ec3"
  rm -rf "$TMP"/*
  git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TMP"
fi

if [ ! -f "$TMP/main.py" ]; then
  echo "ОШИБКА: main.py не найден после клонирования"
  exit 1
fi

mkdir -p "$INSTALL_DIR"
rsync -a \
  --exclude='.git' --exclude='venv' --exclude='__pycache__' \
  --exclude='storage' --exclude='logs' --exclude='config/settings.json' \
  "$TMP/" "$INSTALL_DIR/"

if grep -q 'PARSER_BUILD = "attrs-v6"' "$INSTALL_DIR/services/starvell_catalog.py" 2>/dev/null; then
  echo "✅ Патч attrs-v6 установлен (minimal create + partial-update)"
elif grep -q 'PARSER_BUILD = "attrs-v5"' "$INSTALL_DIR/services/starvell_catalog.py" 2>/dev/null; then
  echo "⚠️  attrs-v5 — обновите до attrs-v6"
else
  echo "⚠️  Старый код парсера — обновите ветку $REPO_BRANCH"
fi
echo "attrs-v6" > "$INSTALL_DIR/PARSER_BUILD.txt" 2>/dev/null || true

mkdir -p "$INSTALL_DIR"/{config,storage/plugins,logs,plugins}
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"
chmod 755 "$INSTALL_DIR"/{config,storage,logs,plugins} 2>/dev/null || true
if [ -d /opt ] && ! sudo -u "$SERVICE_USER" test -x /opt; then
  chmod o+x /opt
fi
if ! sudo -u "$SERVICE_USER" test -x "$INSTALL_DIR"; then
  echo "ОШИБКА: пользователь $SERVICE_USER не может войти в $INSTALL_DIR"
  ls -la "$INSTALL_DIR" /opt 2>/dev/null || true
  exit 1
fi

if [ ! -f "$INSTALL_DIR/venv/bin/pip" ]; then
  echo "Создаю venv …"
  sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
fi
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

if [ ! -f "$INSTALL_DIR/config/settings.json" ]; then
  if [ -f "$INSTALL_DIR/config/settings.json.example" ]; then
    cp "$INSTALL_DIR/config/settings.json.example" "$INSTALL_DIR/config/settings.json"
    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/config/settings.json"
    chmod 600 "$INSTALL_DIR/config/settings.json"
    echo "⚠️  Создан config/settings.json — укажите bot_token!"
  fi
fi

cat > /etc/systemd/system/starvell-cardinal.service <<EOF
[Unit]
Description=Starvell Cardinal Telegram Bot
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$INSTALL_DIR/config/env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=starvell-cardinal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable starvell-cardinal.service 2>/dev/null || true
systemctl restart starvell-cardinal.service
sleep 5

echo ""
echo "=== Проверка BOT_TOKEN ==="
if sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/scripts/check_telegram_token.py"; then
  echo "✅ Токен Telegram валиден"
else
  echo "❌ Проблема с BOT_TOKEN — см. выше"
  echo "   Файл: $INSTALL_DIR/config/settings.json  (поле bot_token)"
  echo "   Или:  $INSTALL_DIR/config/env  (строка BOT_TOKEN=...)"
fi

if systemctl is-active --quiet starvell-cardinal.service; then
  echo "✅ Сервис запущен"
else
  echo "❌ Сервис не стартовал. Логи:"
  journalctl -u starvell-cardinal.service -n 30 --no-pager || true
  exit 1
fi

systemctl status starvell-cardinal.service --no-pager || true
echo "Готово. Проверьте бота: /menu"
