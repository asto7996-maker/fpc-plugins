#!/bin/bash
# Установка/обновление VexBoost AutoSMM для FunPay Cardinal
# Использование: bash install_vexboost.sh /home/fpc/FunPayCardinal

set -e

FPC_DIR="${1:-/home/fpc/FunPayCardinal}"
PLUGIN_URL="https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/vexboost-autosmm-plugin-00fa/plugins/vexboost_autosmm.py"
PLUGIN_FILE="$FPC_DIR/plugins/vexboost_autosmm.py"

echo "=== Установка VexBoost AutoSMM ==="
echo "Папка Cardinal: $FPC_DIR"

if [ ! -d "$FPC_DIR/plugins" ]; then
    echo "ОШИБКА: папка $FPC_DIR/plugins не найдена"
    exit 1
fi

# Резервная копия
if [ -f "$PLUGIN_FILE" ]; then
    cp "$PLUGIN_FILE" "$PLUGIN_FILE.bak.$(date +%Y%m%d_%H%M%S)"
    echo "Старая версия сохранена в .bak"
fi

# Скачивание
echo "Скачиваю плагин..."
curl -fsSL "$PLUGIN_URL" -o "$PLUGIN_FILE"

# Проверка обязательных полей
echo "Проверка файла..."
for field in SETTINGS_PAGE BIND_TO_DELETE VERSION UUID; do
    if ! grep -q "^${field} " "$PLUGIN_FILE" && ! grep -q "^${field}=" "$PLUGIN_FILE"; then
        echo "ОШИБКА: поле $field не найдено в файле!"
        exit 1
    fi
done

VERSION=$(grep '^VERSION = ' "$PLUGIN_FILE" | head -1)
echo "Установлено: $VERSION"

# Удаление кэша
rm -rf "$FPC_DIR/plugins/__pycache__"
find "$FPC_DIR/plugins" -name "vexboost_autosmm*.pyc" -delete 2>/dev/null || true

echo ""
echo "=== Готово! ==="
echo "1. Перезапустите бота: /restart в Telegram"
echo "2. Настройте API: /vexboost → API KEY"
echo "3. Проверьте: grep SETTINGS_PAGE $PLUGIN_FILE"
