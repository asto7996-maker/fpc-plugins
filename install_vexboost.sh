#!/bin/bash
# Установка/обновление VexBoost AutoSMM для FunPay Cardinal
# Автор плагина: @xei1y
# Использование: bash install_vexboost.sh /path/to/FunPayCardinal [URL_плагина]

set -e

FPC_DIR="${1:?Укажите путь к FunPayCardinal}"
PLUGIN_URL="${2:-}"
PLUGIN_FILE="$FPC_DIR/plugins/vexboost_autosmm.py"

if [ -z "$PLUGIN_URL" ]; then
    echo "Укажите URL raw-файла плагина вторым аргументом."
    echo "Пример: bash install_vexboost.sh /opt/FunPayCardinal https://example.com/vexboost_autosmm.py"
    exit 1
fi

echo "=== Установка VexBoost AutoSMM ==="
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

grep -E '^(VERSION|CREDITS) ' "$PLUGIN_FILE" | head -2

rm -rf "$FPC_DIR/plugins/__pycache__"
find "$FPC_DIR/plugins" -name "vexboost_autosmm*.pyc" -delete 2>/dev/null || true

echo ""
echo "Готово. Перезапустите бота: /restart"
echo "Настройка: /vexboost"
