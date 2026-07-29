#!/usr/bin/env bash
# =============================================================================
# MangaBuff Autopilot — установка на сервер (Ubuntu/Debian)
#
#   curl -fsSL https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/remanga-autobattle-ba83/remanga_autobattle/install.sh | bash
# =============================================================================

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/asto7996-maker/fpc-plugins.git}"
REPO_BRANCH="${REPO_BRANCH:-cursor/remanga-autobattle-ba83}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/remanga_autobattle}"
SERVICE_NAME="remanga-autobattle"
MIN_FREE_MB="${MIN_FREE_MB:-2048}"

echo "=============================================="
echo " MangaBuff Autopilot — установка"
echo "=============================================="

free_mb() {
  df -Pm / | awk 'NR==2 {print $4}'
}

echo "[0/6] Проверка места на диске..."
FREE="$(free_mb)"
echo "  Свободно на / : ${FREE} МБ (нужно ≥ ${MIN_FREE_MB} МБ)"

if [[ "$FREE" -lt "$MIN_FREE_MB" ]]; then
  echo
  echo "⚠️  Мало места. Пробую очистить кэши apt/journal..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get clean || true
    sudo rm -rf /var/lib/apt/lists/* || true
    sudo rm -rf /var/cache/apt/archives/* || true
  fi
  sudo journalctl --vacuum-size=50M >/dev/null 2>&1 || true
  sudo rm -rf /tmp/* 2>/dev/null || true
  rm -rf "$HOME/.cache/pip" "$HOME/.cache/ms-playwright" 2>/dev/null || true

  FREE="$(free_mb)"
  echo "  После очистки: ${FREE} МБ"
  if [[ "$FREE" -lt "$MIN_FREE_MB" ]]; then
    echo "❌ Недостаточно места (${FREE} МБ < ${MIN_FREE_MB} МБ)."
    exit 1
  fi
fi

echo "[1/6] Системные пакеты..."
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq 2>/dev/null || echo "  ⚠ apt update пропущен"
  if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      git python3 python3-venv python3-pip 2>/dev/null; then
    command -v git >/dev/null || { echo "Нужен git"; exit 1; }
    command -v python3 >/dev/null || { echo "Нужен python3"; exit 1; }
  fi
fi

TMP_CLONE="$(mktemp -d)"
cleanup() { rm -rf "$TMP_CLONE"; }
trap cleanup EXIT

echo "[2/6] Клонирую репозиторий ($REPO_BRANCH)..."
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$TMP_CLONE" >/dev/null

mkdir -p "$INSTALL_DIR"
KEEP_ENV=""
KEEP_SETTINGS=""
KEEP_MB_STATS=""
[[ -f "$INSTALL_DIR/.env" ]] && KEEP_ENV="$(mktemp)" && cp "$INSTALL_DIR/.env" "$KEEP_ENV"
[[ -f "$INSTALL_DIR/settings.json" ]] && KEEP_SETTINGS="$(mktemp)" && cp "$INSTALL_DIR/settings.json" "$KEEP_SETTINGS"
[[ -f "$INSTALL_DIR/mangabuff_stats.json" ]] && KEEP_MB_STATS="$(mktemp)" && cp "$INSTALL_DIR/mangabuff_stats.json" "$KEEP_MB_STATS"
find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
  ! -name 'user_data_mangabuff' \
  ! -name '.venv' \
  ! -name '.env' \
  ! -name 'settings.json' \
  ! -name 'mangabuff_stats.json' \
  -exec rm -rf {} + 2>/dev/null || true
# убрать старый профиль Remanga, если остался
rm -rf "$INSTALL_DIR/user_data" "$INSTALL_DIR/stats.json" "$INSTALL_DIR/.remanga_token.json" 2>/dev/null || true
cp -a "$TMP_CLONE/remanga_autobattle/." "$INSTALL_DIR/"
[[ -n "$KEEP_ENV" ]] && cp "$KEEP_ENV" "$INSTALL_DIR/.env" && rm -f "$KEEP_ENV"
[[ -n "$KEEP_SETTINGS" ]] && cp "$KEEP_SETTINGS" "$INSTALL_DIR/settings.json" && rm -f "$KEEP_SETTINGS"
[[ -n "$KEEP_MB_STATS" ]] && cp "$KEEP_MB_STATS" "$INSTALL_DIR/mangabuff_stats.json" && rm -f "$KEEP_MB_STATS"

cd "$INSTALL_DIR"

echo "[3/6] venv + pip..."
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo "[4/6] Chromium (Playwright)..."
playwright install-deps chromium 2>/dev/null || echo "  ⚠ install-deps пропущен"
playwright install chromium

echo "[5/6] BOT_TOKEN"
if [[ -f .env ]] && grep -qE '^BOT_TOKEN=.+' .env 2>/dev/null; then
  echo "  .env уже есть — токен не трогаю."
else
  if [[ -n "${BOT_TOKEN:-}" ]]; then
    TOKEN="$BOT_TOKEN"
  else
    echo
    echo "Введите токен бота от @BotFather:"
    if [[ -r /dev/tty ]]; then
      read -r -p "BOT_TOKEN: " TOKEN </dev/tty
    else
      read -r -p "BOT_TOKEN: " TOKEN
    fi
  fi
  if [[ -z "${TOKEN// }" ]]; then
    echo "Ошибка: BOT_TOKEN пустой." >&2
    exit 1
  fi
  cat > .env <<EOF
BOT_TOKEN=$TOKEN
EOF
  echo "  .env записан."
fi

echo "[6/6] systemd..."
RUN_USER="${SUDO_USER:-$USER}"
if [[ "$(id -u)" -eq 0 ]]; then
  RUN_USER="root"
fi
if [[ "$RUN_USER" == "root" ]]; then
  RUN_HOME="/root"
else
  RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
fi
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="$INSTALL_DIR/.venv/bin/python"
BOT_PY="$INSTALL_DIR/bot.py"

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=MangaBuff Autopilot Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
Environment=HOME=$RUN_HOME
ExecStart=$PYTHON_BIN $BOT_PY
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null
sudo systemctl restart "$SERVICE_NAME"

echo
echo "=============================================="
echo " Установка завершена: $INSTALL_DIR"
echo "=============================================="
echo
echo "1) Telegram: /start"
echo "2) Сессия MangaBuff (один раз):"
echo "   sudo systemctl stop $SERVICE_NAME"
echo "   cd $INSTALL_DIR && source .venv/bin/activate && python bot.py --setup-mangabuff"
echo "   sudo systemctl start $SERVICE_NAME"
echo
echo "Логи: sudo journalctl -u $SERVICE_NAME -f"
echo
