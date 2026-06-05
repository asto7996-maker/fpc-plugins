#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Content Bot — Скрипт установки
# Поддерживает Ubuntu/Debian, CentOS/RHEL, Arch Linux
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${BOT_DIR}/venv"
SERVICE_NAME="content-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
    elif [ -f /etc/redhat-release ]; then
        OS="centos"
    else
        OS="unknown"
    fi
    echo "$OS"
}

install_system_deps() {
    local os
    os=$(detect_os)
    info "Обнаружена ОС: $os"

    case "$os" in
        ubuntu|debian)
            info "Установка системных зависимостей (apt)..."
            sudo apt-get update -qq
            sudo apt-get install -y -qq \
                python3 python3-pip python3-venv python3-dev \
                ffmpeg \
                git curl wget
            ;;
        centos|rhel|fedora|rocky|almalinux)
            info "Установка системных зависимостей (yum/dnf)..."
            if command -v dnf &>/dev/null; then
                PKG_MGR="dnf"
            else
                PKG_MGR="yum"
            fi
            sudo $PKG_MGR install -y \
                python3 python3-pip python3-devel \
                ffmpeg \
                git curl wget
            ;;
        arch|manjaro)
            info "Установка системных зависимостей (pacman)..."
            sudo pacman -Sy --noconfirm \
                python python-pip python-virtualenv \
                ffmpeg \
                git curl wget
            ;;
        *)
            warn "Неизвестная ОС ($os). Установите вручную: python3, pip, ffmpeg"
            ;;
    esac
}

create_venv() {
    info "Создание виртуального окружения..."
    python3 -m venv "$VENV_DIR"
    source "${VENV_DIR}/bin/activate"
    pip install --upgrade pip setuptools wheel
    pip install -r "${BOT_DIR}/requirements.txt"
    info "Зависимости Python установлены"
}

setup_config() {
    if [ ! -f "${BOT_DIR}/config.yaml" ]; then
        cp "${BOT_DIR}/config.example.yaml" "${BOT_DIR}/config.yaml"
        info "Создан config.yaml из шаблона"
        warn "ОБЯЗАТЕЛЬНО отредактируйте config.yaml перед запуском!"
        warn "  nano ${BOT_DIR}/config.yaml"
    else
        info "config.yaml уже существует"
    fi
}

create_dirs() {
    mkdir -p "${BOT_DIR}/downloads"
    info "Директории созданы"
}

create_systemd_service() {
    info "Создание systemd-сервиса..."

    local user
    user=$(whoami)

    sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=Telegram Content Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${user}
Group=${user}
WorkingDirectory=${BOT_DIR}
ExecStart=${VENV_DIR}/bin/python ${BOT_DIR}/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Безопасность
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${BOT_DIR}
PrivateTmp=true

# Лимиты
LimitNOFILE=65536
MemoryMax=512M

# Переменные окружения (раскомментируйте при необходимости)
# Environment=TG_API_ID=12345
# Environment=TG_API_HASH=your_hash
# Environment=TG_BOT_TOKEN=your_token

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    info "Systemd-сервис создан: ${SERVICE_NAME}"
}

print_usage() {
    echo ""
    echo "============================================================"
    echo "  Content Bot — Установка завершена!"
    echo "============================================================"
    echo ""
    echo "  Директория: ${BOT_DIR}"
    echo "  Виртуальное окружение: ${VENV_DIR}"
    echo ""
    echo "  1. Отредактируйте конфигурацию:"
    echo "     nano ${BOT_DIR}/config.yaml"
    echo ""
    echo "  2. Первый запуск (авторизация):"
    echo "     cd ${BOT_DIR}"
    echo "     source venv/bin/activate"
    echo "     python main.py"
    echo ""
    echo "  3. Управление сервисом:"
    echo "     sudo systemctl start ${SERVICE_NAME}"
    echo "     sudo systemctl stop ${SERVICE_NAME}"
    echo "     sudo systemctl restart ${SERVICE_NAME}"
    echo "     sudo systemctl status ${SERVICE_NAME}"
    echo "     sudo systemctl enable ${SERVICE_NAME}  # автозапуск"
    echo ""
    echo "  4. Логи:"
    echo "     journalctl -u ${SERVICE_NAME} -f"
    echo "     tail -f ${BOT_DIR}/bot.log"
    echo ""
    echo "============================================================"
}

main() {
    info "Начало установки Content Bot..."
    echo ""

    install_system_deps
    create_venv
    setup_config
    create_dirs
    create_systemd_service
    print_usage
}

main "$@"
