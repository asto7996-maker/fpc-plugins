#!/usr/bin/env bash
# =============================================================================
# Remanga Autobattle — установка на сервер (Ubuntu/Debian)
#
# Одна команда:
#   curl -fsSL https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/remanga-autobattle-ba83/remanga_autobattle/install.sh | bash
#
# Или из уже склонированного репо:
#   bash remanga_autobattle/install.sh
#
# Скрипт спросит только BOT_TOKEN (@BotFather).
# Остальные настройки вводятся в Telegram после /start.
# =============================================================================

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/asto7996-maker/fpc-plugins.git}"
REPO_BRANCH="${REPO_BRANCH:-cursor/remanga-autobattle-ba83}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/remanga_autobattle}"
SERVICE_NAME="remanga-autobattle"

echo "=============================================="
echo " Remanga Autobattle — установка"
echo "=============================================="

# --- зависимости ОС ---
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git python3 python3-venv python3-pip \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2t64 libpango-1.0-0 libcairo2 \
    >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git python3 python3-venv python3-pip \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    >/dev/null
fi

# --- код ---
TMP_CLONE="$(mktemp -d)"
cleanup() { rm -rf "$TMP_CLONE"; }
trap cleanup EXIT

echo "[1/6] Клонирую репозиторий ($REPO_BRANCH)..."
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TMP_CLONE" >/dev/null 2>&1

mkdir -p "$INSTALL_DIR"
# Сохраняем локальные данные при обновлении
KEEP_ENV=""
KEEP_SETTINGS=""
[[ -f "$INSTALL_DIR/.env" ]] && KEEP_ENV="$(mktemp)" && cp "$INSTALL_DIR/.env" "$KEEP_ENV"
[[ -f "$INSTALL_DIR/settings.json" ]] && KEEP_SETTINGS="$(mktemp)" && cp "$INSTALL_DIR/settings.json" "$KEEP_SETTINGS"
# Копируем код (без затирания user_data)
find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
  ! -name 'user_data' ! -name '.venv' ! -name '.env' ! -name 'settings.json' \
  -exec rm -rf {} +
cp -a "$TMP_CLONE/remanga_autobattle/." "$INSTALL_DIR/"
[[ -n "$KEEP_ENV" ]] && cp "$KEEP_ENV" "$INSTALL_DIR/.env" && rm -f "$KEEP_ENV"
[[ -n "$KEEP_SETTINGS" ]] && cp "$KEEP_SETTINGS" "$INSTALL_DIR/settings.json" && rm -f "$KEEP_SETTINGS"

cd "$INSTALL_DIR"

# --- venv + pip ---
echo "[2/6] Создаю venv и ставлю зависимости..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "[3/6] Устанавливаю Chromium для Playwright..."
playwright install chromium
playwright install-deps chromium >/dev/null 2>&1 || true

# --- токен ---
echo "[4/6] Настройки запуска"
if [[ -f .env ]] && grep -q '^BOT_TOKEN=.\+' .env 2>/dev/null; then
  echo "  .env уже есть — BOT_TOKEN не перезаписываю."
else
  if [[ -n "${BOT_TOKEN:-}" ]]; then
    TOKEN="$BOT_TOKEN"
  else
    echo
    echo "Введите токен бота от @BotFather:"
    read -r -p "BOT_TOKEN: " TOKEN
  fi
  if [[ -z "${TOKEN// }" ]]; then
    echo "Ошибка: BOT_TOKEN пустой." >&2
    exit 1
  fi
  cat > .env <<EOF
# Только токен нужен для старта.
# Admin ID, URL боёв, интервал и таймаут — вводятся в Telegram (/start → мастер).
BOT_TOKEN=$TOKEN
EOF
  echo "  .env записан."
fi

# --- systemd ---
echo "[5/6] systemd-сервис..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="$INSTALL_DIR/.venv/bin/python"
BOT_PY="$INSTALL_DIR/bot.py"

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Remanga Autobattle Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON_BIN $BOT_PY
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null
sudo systemctl restart "$SERVICE_NAME"

echo "[6/6] Готово."
echo
echo "=============================================="
echo " Установка завершена: $INSTALL_DIR"
echo "=============================================="
echo
echo "1) Откройте бота в Telegram и нажмите /start"
echo "   — мастер спросит URL / интервал / таймаут"
echo "   — вы станете админом автоматически"
echo
echo "2) Один раз сохраните сессию Remanga (нужен GUI или VNC):"
echo "   cd $INSTALL_DIR && source .venv/bin/activate && python bot.py --setup"
echo "   (после setup снова: sudo systemctl start $SERVICE_NAME)"
echo
echo "Полезные команды:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo "  sudo systemctl restart $SERVICE_NAME"
echo
