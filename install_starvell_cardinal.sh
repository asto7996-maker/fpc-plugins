#!/bin/bash
# Starvell Cardinal — установка на Ubuntu 24.04
# Использование: curl -fsSL <url>/install_starvell_cardinal.sh | sudo bash
# Или: sudo bash install_starvell_cardinal.sh

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/starvell-cardinal}"
SERVICE_USER="${SERVICE_USER:-starvell}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

echo "=============================================="
echo "  Starvell Cardinal — установка"
echo "  Ubuntu 24.04"
echo "=============================================="

if [ "$EUID" -ne 0 ]; then
    echo "Запустите с правами root: sudo bash install_starvell_cardinal.sh"
    exit 1
fi

echo ""
echo "=== 1. Системные пакеты ==="
apt-get update -qq
apt-get install -y \
    python${PYTHON_VERSION} \
    python${PYTHON_VERSION}-venv \
    python3-pip \
    git \
    curl \
    sqlite3 \
    screen \
  2>/dev/null || apt-get install -y python3 python3-venv python3-pip git curl sqlite3 screen

echo ""
echo "=== 2. Пользователь и каталог ==="
id "$SERVICE_USER" &>/dev/null || useradd -r -m -s /bin/bash "$SERVICE_USER"
mkdir -p "$INSTALL_DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/main.py" ]; then
    echo "Копирую файлы из $SCRIPT_DIR …"
    rsync -a --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
        "$SCRIPT_DIR/" "$INSTALL_DIR/" 2>/dev/null || cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
else
    echo "Клонирую репозиторий…"
    git clone --depth 1 https://github.com/YOUR_REPO/starvell-cardinal.git "$INSTALL_DIR" 2>/dev/null || {
        echo "Создаю пустой каталог — скопируйте файлы бота в $INSTALL_DIR"
    }
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo ""
echo "=== 3. Виртуальное окружение Python ==="
sudo -u "$SERVICE_USER" python${PYTHON_VERSION} -m venv "$INSTALL_DIR/venv" 2>/dev/null || \
    sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo ""
echo "=== 4. Каталоги данных ==="
sudo -u "$SERVICE_USER" mkdir -p "$INSTALL_DIR"/{config,storage/plugins,logs,plugins}

echo ""
echo "=== 5. Конфигурация (интерактивно) ==="
read -rp "Telegram BOT_TOKEN (@BotFather): " BOT_TOKEN
read -rsp "Пароль для входа в бота: " BOT_PASSWORD
echo ""
read -rp "SESSION_COOKIE Starvell (cookie 'session'): " SESSION_COOKIE
read -rp "Gemini API ключ (Enter — пропустить): " GEMINI_KEY
read -rp "OpenAI API ключ (Enter — пропустить): " OPENAI_KEY
read -rp "Ваш Telegram user_id (для админ-доступа): " ADMIN_ID

PASS_MD5=$(echo -n "$BOT_PASSWORD" | md5sum | awk '{print $1}')

cat > "$INSTALL_DIR/config/settings.json" <<EOF
{
  "bot_token": "$BOT_TOKEN",
  "bot_password_md5": "$PASS_MD5",
  "admin_ids": [${ADMIN_ID:-0}],
  "session_cookie": "$SESSION_COOKIE",
  "auto_delivery_enabled": true,
  "auto_bump_enabled": true,
  "auto_welcome_enabled": true,
  "auto_review_enabled": true,
  "ai_replies_enabled": true,
  "gemini_api_key": "$GEMINI_KEY",
  "openai_api_key": "$OPENAI_KEY",
  "ai_provider": "gemini",
  "bump_interval": 3600,
  "chat_poll_interval": 5,
  "orders_poll_interval": 10,
  "api_delay_seconds": 1.5
}
EOF
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/config/settings.json"
chmod 600 "$INSTALL_DIR/config/settings.json"

echo ""
echo "=== 6. Systemd-сервис ==="
cat > /etc/systemd/system/starvell-cardinal.service <<EOF
[Unit]
Description=Starvell Cardinal Telegram Bot
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable starvell-cardinal.service
systemctl restart starvell-cardinal.service

echo ""
echo "=============================================="
echo "  Установка завершена!"
echo "=============================================="
echo ""
echo "  Каталог:    $INSTALL_DIR"
echo "  Конфиг:     $INSTALL_DIR/config/settings.json"
echo "  Логи:       $INSTALL_DIR/logs/cardinal.log"
echo "  Плагины:    $INSTALL_DIR/plugins/"
echo ""
echo "  Команды:"
echo "    systemctl status starvell-cardinal"
echo "    systemctl restart starvell-cardinal"
echo "    journalctl -u starvell-cardinal -f"
echo ""
echo "  Telegram: откройте бота → /start → введите пароль"
echo ""
echo "  Альтернатива (screen):"
echo "    screen -S cardinal"
echo "    cd $INSTALL_DIR && ./venv/bin/python main.py"
echo "    Ctrl+A, D — отключиться"
echo ""
