#!/usr/bin/env bash
# =============================================================================
# Telegram Content Bot — Server Installation Script
# Tested on Ubuntu 22.04 / 24.04 LTS
# Run as: sudo bash install.sh
# =============================================================================

set -euo pipefail

# ---- Configuration ----------------------------------------------------------
BOT_USER="tgbot"
BOT_DIR="/opt/tg_content_bot"
SERVICE_NAME="tg_content_bot"
PYTHON_MIN="3.10"
# -----------------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Must be root
[[ $EUID -eq 0 ]] || error "Run as root: sudo bash install.sh"

# ---- 1. System packages -----------------------------------------------------
info "Updating package lists..."
apt-get update -qq

info "Installing system dependencies..."
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    ffmpeg \
    git \
    curl \
    wget \
    build-essential \
    libssl-dev \
    libffi-dev \
    libsqlite3-dev \
    tzdata \
    supervisor \
    2>&1 | tail -5

# Verify Python version
PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python version: $PYTHON_VERSION"
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    info "Python $PYTHON_VERSION OK"
else
    warn "Python 3.10+ recommended. Installing from deadsnakes PPA..."
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1
fi

# ---- 2. Create system user --------------------------------------------------
if ! id "$BOT_USER" &>/dev/null; then
    info "Creating system user: $BOT_USER"
    useradd --system --create-home --shell /bin/bash "$BOT_USER"
else
    info "User $BOT_USER already exists"
fi

# ---- 3. Create bot directory ------------------------------------------------
info "Setting up bot directory: $BOT_DIR"
mkdir -p "$BOT_DIR"
mkdir -p "$BOT_DIR"/{sessions,media,logs,data}

# Copy project files (assumes install.sh is in the project directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Copying project files from $SCRIPT_DIR to $BOT_DIR..."
cp -r "$SCRIPT_DIR"/*.py   "$BOT_DIR/" 2>/dev/null || true
cp -r "$SCRIPT_DIR"/requirements.txt "$BOT_DIR/"
cp -r "$SCRIPT_DIR"/.env.example "$BOT_DIR/.env.example"

# Set up .env if it doesn't exist
if [[ ! -f "$BOT_DIR/.env" ]]; then
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    warn "Created $BOT_DIR/.env from example. Edit it before starting the bot!"
fi

# ---- 4. Python virtual environment ------------------------------------------
info "Creating Python virtual environment..."
python3 -m venv "$BOT_DIR/venv"
source "$BOT_DIR/venv/bin/activate"

info "Upgrading pip..."
pip install --upgrade pip wheel setuptools -q

info "Installing Python dependencies..."
pip install -r "$BOT_DIR/requirements.txt" -q

deactivate
info "Dependencies installed"

# ---- 5. Permissions ---------------------------------------------------------
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"
chmod 750 "$BOT_DIR"
chmod 600 "$BOT_DIR/.env" 2>/dev/null || true
chmod +x "$BOT_DIR/main.py"

# ---- 6. Systemd service -----------------------------------------------------
info "Creating systemd service: $SERVICE_NAME"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Telegram Content & Marketing Bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${BOT_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
EnvironmentFile=${BOT_DIR}/.env
ExecStart=${BOT_DIR}/venv/bin/python ${BOT_DIR}/main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${BOT_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
info "Systemd service created"

# ---- 7. Logrotate -----------------------------------------------------------
cat > "/etc/logrotate.d/${SERVICE_NAME}" <<EOF
${BOT_DIR}/logs/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 ${BOT_USER} ${BOT_USER}
    postrotate
        systemctl kill -s HUP ${SERVICE_NAME} 2>/dev/null || true
    endscript
}
EOF
info "Logrotate configured"

# ---- 8. UFW firewall (optional) ---------------------------------------------
if command -v ufw &>/dev/null; then
    ufw allow ssh comment 'SSH' 2>/dev/null || true
    info "UFW: ensured SSH is allowed"
fi

# ---- 9. Print summary -------------------------------------------------------
echo ""
echo "============================================================"
echo -e "${GREEN}Installation complete!${NC}"
echo "============================================================"
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. Edit the configuration file:"
echo "   sudo nano $BOT_DIR/.env"
echo ""
echo "2. Authorise the main Telethon account (run once interactively):"
echo "   sudo -u $BOT_USER $BOT_DIR/venv/bin/python $BOT_DIR/main.py --login"
echo ""
echo "3. (Optional) Add worker session files to $BOT_DIR/sessions/"
echo "   Each .session file = one worker account for inviting/DM."
echo "   Use the session_login.py helper to generate them."
echo ""
echo "4. (Optional) Add proxies to $BOT_DIR/proxies.txt"
echo "   Format: socks5://user:pass@host:port  (one per line)"
echo ""
echo "5. Enable and start the bot:"
echo "   sudo systemctl enable $SERVICE_NAME"
echo "   sudo systemctl start  $SERVICE_NAME"
echo ""
echo "6. Check the service status:"
echo "   sudo systemctl status $SERVICE_NAME"
echo "   sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "Bot directory:  $BOT_DIR"
echo "Bot user:       $BOT_USER"
echo "Service name:   $SERVICE_NAME"
echo ""
