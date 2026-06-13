#!/bin/bash
# Установка/обновление VexBoost AutoSMM для Starvell Cardinal
# Использование: bash install_vexboost.sh /path/to/starvell-cardinal [URL_плагина]

set -e

BOT_DIR="${1:?Укажите путь к Starvell Cardinal}"
PLUGIN_URL="${2:-}"
PLUGIN_FILE="$BOT_DIR/plugins/vexboost_autosmm.py"

if [ -z "$PLUGIN_URL" ]; then
    echo "Укажите URL raw-файла плагина вторым аргументом."
    echo "Пример: bash install_vexboost.sh /opt/starvell-cardinal https://raw.githubusercontent.com/.../vexboost_autosmm.py"
    exit 1
fi

echo "=== Установка VexBoost AutoSMM (Starvell) ==="
echo "Папка бота: $BOT_DIR"

if [ ! -d "$BOT_DIR/plugins" ]; then
    echo "ОШИБКА: папка $BOT_DIR/plugins не найдена"
    exit 1
fi

if [ -f "$PLUGIN_FILE" ]; then
    cp "$PLUGIN_FILE" "$PLUGIN_FILE.bak.$(date +%Y%m%d_%H%M%S)"
    echo "Резервная копия сохранена (.bak)"
fi

echo "Скачиваю плагин..."
curl -fsSL "$PLUGIN_URL" -o "$PLUGIN_FILE"

for field in SETTINGS_PAGE VERSION UUID; do
    if ! grep -qE "^${field} " "$PLUGIN_FILE" && ! grep -qE "^${field}=" "$PLUGIN_FILE"; then
        echo "ОШИБКА: поле $field не найдено в файле!"
        exit 1
    fi
done

grep -E '^(VERSION|NAME) ' "$PLUGIN_FILE" | head -2

rm -rf "$BOT_DIR/plugins/__pycache__"
find "$BOT_DIR/plugins" -name "vexboost_autosmm*.pyc" -delete 2>/dev/null || true

echo ""
echo "Готово. Перезагрузите плагины в Telegram или перезапустите бота."
echo "Настройка: Плагины → VexBoost AutoSMM → Настройки (или /vexboost)"
