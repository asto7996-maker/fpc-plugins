#!/usr/bin/env bash
# Запуск VK VPN-бота Paskod
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  echo "Нет .env — копирую из .env.example"
  cp .env.example .env
  echo "Заполните VK_TOKEN и BEDOLAGA_API_KEY в .env"
  exit 1
fi

python3 -m pip install -q -r requirements.txt
exec python3 main.py
