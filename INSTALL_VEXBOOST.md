# VexBoost AutoSMM — установка

**Автор плагина:** @xei1y

## 1. Зависимости

```bash
pip install requests pyTelegramBotAPI
```

Если Cardinal в venv — установите пакеты внутри venv.

## 2. Установка плагина

Скопируйте `plugins/vexboost_autosmm.py` в папку `plugins` вашего FunPay Cardinal.

Или через curl (подставьте свой URL файла):

```bash
curl -fsSL -o /path/to/FunPayCardinal/plugins/vexboost_autosmm.py "URL_ФАЙЛА"
rm -rf /path/to/FunPayCardinal/plugins/__pycache__
```

## 3. Перезапуск

В Telegram-боте Cardinal:

```
/restart
```

## 4. Настройка

```
/vexboost
```

- **URL** — адрес SMM-панели (например `https://vexboost.ru`)
- **Логин + пароль** — данные аккаунта панели (рекомендуется)
- Либо **AuthToken** / **API KEY** — альтернативные режимы

Проверка: `/vb_balance`

## 5. Лот FunPay

В описании лота:

```
ID: 1000
#Quan: 1
```

`ID` — ID услуги в SMM-панели. `#Quan` — множитель количества (опционально).
