# Установка VexBoost AutoSMM на Ubuntu 24.04

## 1. Системные зависимости

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git curl
```

## 2. Python-библиотеки для плагина

FunPay Cardinal уже включает большинство зависимостей. Для плагина дополнительно нужны:

```bash
pip3 install requests pyTelegramBotAPI
```

Если Cardinal установлен в виртуальном окружении — активируйте его и установите там:

```bash
cd /home/fpc/FunPayCardinal
source venv/bin/activate   # или: source .venv/bin/activate
pip install requests pyTelegramBotAPI
```

## 3. Установка плагина

> **Важно:** репозиторий приватный — `curl` с raw.githubusercontent.com вернёт **404**.
> Используйте `git clone` (см. ниже).

### Способ A — через git clone (рекомендуется)

```bash
cd /tmp
git clone https://github.com/asto7996-maker/fpc-plugins.git
cp fpc-plugins/plugins/vexboost_autosmm.py /home/fpc/FunPayCardinal/plugins/
rm -rf /home/fpc/FunPayCardinal/plugins/__pycache__
grep -E "^(VERSION|SETTINGS_PAGE)" /home/fpc/FunPayCardinal/plugins/vexboost_autosmm.py
```

Должно быть:
```
VERSION = "2.0.2"
SETTINGS_PAGE = False
```

Если `git clone` просит авторизацию — используйте Personal Access Token:
```bash
git clone https://<ВАШ_TOKEN>@github.com/asto7996-maker/fpc-plugins.git
```

### Способ B — скрипт установки (только для публичного репо)

```bash
bash install_vexboost.sh /home/fpc/FunPayCardinal
```

## 4. Перезапуск бота

В Telegram-боте Cardinal:
```
/restart
```

Или через консоль:
```bash
cd /home/fpc/FunPayCardinal
python3 main.py
```

## 5. Настройка

1. В Telegram-боте Cardinal: `/vexboost`
2. Нажмите **API KEY** → вставьте ключ из [vexboost.ru](https://vexboost.ru/)
3. API URL по умолчанию: `https://vexboost.ru/api/v2`

## 6. Настройка лотов FunPay

В описании каждого лота добавьте:

```
ID: 1634
#Quan: 10
```

- `ID:` — ID услуги на VexBoost (обязательно)
- `#Quan:` — множитель количества (опционально)

## 7. Команды

| Команда | Описание |
|---------|----------|
| `/vexboost` | Панель управления |
| `/vb_stats` | Статистика и прибыль |
| `/vb_balance` | Баланс VexBoost и FunPay |

Покупатель в чате FunPay:
- `#статус 12345` — статус заказа VexBoost
- `#рефилл 12345` — запрос рефилла

## 8. Проверка работы

```bash
# В логах Cardinal должно появиться:
# VexBoost AutoSMM v2.0.0 загружен.

# В Telegram: /vexboost → Диагностика — все пункты ✅
```

## 9. Устранение неполадок

```bash
# Ошибка загрузки плагина
rm -rf /home/fpc/FunPayCardinal/plugins/__pycache__
grep SETTINGS_PAGE /home/fpc/FunPayCardinal/plugins/vexboost_autosmm.py

# Ошибка API
# Проверьте ключ и баланс на vexboost.ru

# Данные плагина
ls storage/plugins/a3f8c2e1-7b4d-4a9f-9e2c-1d5b8f6a0c3e/
```
