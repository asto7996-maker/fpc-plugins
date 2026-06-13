#!/bin/bash
# Starvell Cardinal — установка на Ubuntu 24.04 / Debian
#
# Одной командой (скачать и установить):
#   curl -fsSL https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/starvell-cardinal-bot-280c/install_starvell_cardinal.sh | sudo bash
#
# Или из уже скачанного репозитория:
#   cd fpc-plugins && sudo bash install_starvell_cardinal.sh

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/starvell-cardinal}"
SERVICE_USER="${SERVICE_USER:-starvell}"
REPO_URL="${REPO_URL:-https://github.com/asto7996-maker/fpc-plugins.git}"
REPO_BRANCH="${REPO_BRANCH:-cursor/starvell-cardinal-bot-280c}"
TMP_CLONE=""

echo "=============================================="
echo "  Starvell Cardinal — установка"
echo "=============================================="

if [ "$EUID" -ne 0 ]; then
    echo "ОШИБКА: запустите от root:"
    echo "  sudo bash install_starvell_cardinal.sh"
    exit 1
fi

# ── 1. Системные пакеты ───────────────────────────────────────────────────
echo ""
echo "=== 1/6. Системные пакеты ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    curl \
    sqlite3 \
    screen \
    rsync \
    ca-certificates

# Определяем Python (Ubuntu 24.04 по умолчанию — python3.12)
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="python3.12"
elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="python3.11"
else
    echo "ОШИБКА: Python 3 не найден после установки пакетов"
    exit 1
fi
echo "Используется: $($PYTHON_BIN --version)"

# ── 2. Каталог и исходники ─────────────────────────────────────────────────
echo ""
echo "=== 2/6. Загрузка проекта ==="
id "$SERVICE_USER" &>/dev/null || useradd -r -m -s /bin/bash "$SERVICE_USER"
mkdir -p "$INSTALL_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
SOURCE_DIR=""

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/main.py" ]; then
    SOURCE_DIR="$SCRIPT_DIR"
    echo "Копирую файлы из $SOURCE_DIR …"
elif [ -d "$INSTALL_DIR/.git" ] && [ -f "$INSTALL_DIR/main.py" ]; then
    SOURCE_DIR="$INSTALL_DIR"
    echo "Проект уже есть в $INSTALL_DIR"
else
    echo "Клонирую репозиторий ($REPO_BRANCH) …"
    TMP_CLONE="$(mktemp -d)"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TMP_CLONE"
    SOURCE_DIR="$TMP_CLONE"
fi

if [ "$SOURCE_DIR" != "$INSTALL_DIR" ]; then
    rsync -a --delete \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='venv' \
        --exclude='storage' \
        --exclude='logs' \
        --exclude='config/settings.json' \
        "$SOURCE_DIR/" "$INSTALL_DIR/"
    rm -rf "${TMP_CLONE:-}"
fi

if [ ! -f "$INSTALL_DIR/main.py" ]; then
    echo "ОШИБКА: main.py не найден в $INSTALL_DIR"
    echo "Проверьте доступ к репозиторию и ветку: $REPO_BRANCH"
    exit 1
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── 3. Виртуальное окружение ────────────────────────────────────────────────
echo ""
echo "=== 3/6. Python-зависимости ==="
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ── 4. Каталоги ─────────────────────────────────────────────────────────────
echo ""
echo "=== 4/6. Каталоги данных ==="
sudo -u "$SERVICE_USER" mkdir -p "$INSTALL_DIR"/{config,storage/plugins,logs,plugins}

if [ ! -f "$INSTALL_DIR/config/settings.json" ]; then
    if [ -f "$INSTALL_DIR/config/settings.json.example" ]; then
        sudo -u "$SERVICE_USER" cp "$INSTALL_DIR/config/settings.json.example" "$INSTALL_DIR/config/settings.json"
    fi
fi

# ── 5. Конфигурация ─────────────────────────────────────────────────────────
echo ""
echo "=== 5/6. Настройка (введите данные) ==="
echo "Подсказка: Telegram user_id узнайте у @userinfobot"
echo ""

read -rp "Telegram BOT_TOKEN (@BotFather): " BOT_TOKEN
read -rsp "Пароль для входа в бота: " BOT_PASSWORD
echo ""
read -rp "SESSION_COOKIE Starvell (cookie 'session'): " SESSION_COOKIE
read -rp "Gemini API ключ (Enter — пропустить): " GEMINI_KEY
read -rp "OpenAI API ключ (Enter — пропустить): " OPENAI_KEY
read -rp "Ваш Telegram user_id: " ADMIN_ID

if [ -z "$BOT_TOKEN" ]; then
    echo "ОШИБКА: BOT_TOKEN обязателен"
    exit 1
fi
if [ -z "$BOT_PASSWORD" ]; then
    echo "ОШИБКА: пароль бота обязателен"
    exit 1
fi

PASS_MD5="$(echo -n "$BOT_PASSWORD" | md5sum | awk '{print $1}')"
ADMIN_ID="${ADMIN_ID:-0}"

cat > "$INSTALL_DIR/config/settings.json" <<EOF
{
  "bot_token": "$BOT_TOKEN",
  "bot_password_md5": "$PASS_MD5",
  "admin_ids": [$ADMIN_ID],
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

# ── 6. Systemd ──────────────────────────────────────────────────────────────
echo ""
echo "=== 6/6. Systemd-сервис ==="
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

sleep 2
if systemctl is-active --quiet starvell-cardinal.service; then
    STATUS="запущен ✅"
else
    STATUS="ошибка запуска ❌ — смотрите: journalctl -u starvell-cardinal -n 50"
fi

echo ""
echo "=============================================="
echo "  Установка завершена!"
echo "  Сервис: $STATUS"
echo "=============================================="
echo ""
echo "  Каталог:  $INSTALL_DIR"
echo "  Конфиг:   $INSTALL_DIR/config/settings.json"
echo "  Логи:     $INSTALL_DIR/logs/cardinal.log"
echo ""
echo "  systemctl status starvell-cardinal"
echo "  journalctl -u starvell-cardinal -f"
echo ""
echo "  Telegram → /start → введите пароль"
echo ""
