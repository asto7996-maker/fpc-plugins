#!/bin/bash
# Установка/обновление Gemini Link Auto для FunPay Cardinal
# Использование: bash install_gemini_link.sh /path/to/FunPayCardinal

set -e

FPC_DIR="${1:?Укажите путь к FunPayCardinal, например: bash install_gemini_link.sh /opt/FunPayCardinal}"
PLUGIN_URL="${2:-https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/funpay-gemini-automation-2190/plugins/gemini_link_automation.py}"
PLUGIN_FILE="$FPC_DIR/plugins/gemini_link_automation.py"

echo "=== Установка Gemini Link Auto ==="
echo "Папка Cardinal: $FPC_DIR"

if [ ! -d "$FPC_DIR/plugins" ]; then
    echo "ОШИБКА: папка $FPC_DIR/plugins не найдена"
    exit 1
fi

if [ -f "$PLUGIN_FILE" ]; then
    cp "$PLUGIN_FILE" "$PLUGIN_FILE.bak.$(date +%Y%m%d_%H%M%S)"
    echo "Резервная копия сохранена (.bak)"
fi

echo "Скачиваю плагин..."
curl -fsSL "$PLUGIN_URL" -o "$PLUGIN_FILE"

for field in SETTINGS_PAGE BIND_TO_DELETE VERSION UUID CREDITS; do
    if ! grep -qE "^${field} " "$PLUGIN_FILE" && ! grep -qE "^${field}=" "$PLUGIN_FILE"; then
        echo "ОШИБКА: поле $field не найдено в файле!"
        exit 1
    fi
done

grep -E '^(VERSION|CREDITS|NAME) ' "$PLUGIN_FILE" | head -3

rm -rf "$FPC_DIR/plugins/__pycache__"
find "$FPC_DIR/plugins" -name "gemini_link_automation*.pyc" -delete 2>/dev/null || true

echo ""
echo "✅ Готово!"
echo "1. Перезапустите бота в Telegram: /restart"
echo "2. Настройте API: /gemini_link"
echo "3. Укажите BOT_API_URL и BOT_API_KEY в панели плагина"
